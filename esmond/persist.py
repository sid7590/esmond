#!/usr/bin/env python

import logging
import os
import os.path
import sys
import time
import signal
import errno
import datetime
import cProfile
import pstats
import __main__

from math import floor, ceil
from subprocess import Popen, PIPE, STDOUT

import cPickle as pickle

import json

import django
from django.utils.timezone import now, utc, make_aware

try:
    import tsdb
    import tsdb.row
    from tsdb.error import TSDBError, TSDBAggregateDoesNotExistError, \
            TSDBVarDoesNotExistError, InvalidMetaData
except ImportError:
    tsdb = None

from esmond.util import setproctitle, init_logging, get_logger, \
        remove_metachars,  build_alu_sap_name
from esmond.util import daemonize, setup_exc_handler, max_datetime
from esmond.config import get_opt_parser, get_config, get_config_path
from esmond.error import ConfigError
from esmond.api.dataseries import fit_to_bins

from esmond.api.models import Device, OIDSet, IfRef, ALUSAPRef, LSPOpStatus, \
                              OutletRef

from esmond.cassandra import CASSANDRA_DB, RawRateData, BaseRateBin, AggregationBin, MaximumRetryException


try:
    import cmemcache as memcache
except ImportError:
    try:
        import memcache
    except:
        raise Exception('no memcache library found')

PERSIST_SLEEP_TIME = 1
HEARTBEAT_FREQ_MULTIPLIER = 3

class PollResult(object):
    """PollResult contains the results of a polling run.

    The internals of PollResults may vary on a per subclass implementation,
    however all subclasses must implement a ``pickle`` method as these provide
    a baseline functionality for the generic ``PollPersister`` class.

    ``oidset_name``
        this is used to determine which PollPersisters are used to store this
        PollResult.
    ``prefix``
        the prefix where these results are to be stored
    ``timestamp``
        the timestamp for this PollResult
    ``data``
        the data to be stored, this is opaque at this level but must be
        pickleable.  some PollPersister require a particular format for
        ``data``.
    ``metadata``
        a dict of additional data about this data.  some PollPersisters require
        specific keys to exist in the ``metadata`` dict.
    """
    def __init__(self, oidset_name, device_name, oid_name, timestamp, data,
            metadata, **kwargs):
        self.oidset_name = oidset_name
        self.device_name = device_name
        self.oid_name = oid_name
        self.timestamp = timestamp
        self.data = data
        self.metadata = metadata

    def __str__(self):
        return '%s.%s %d' % (self.device_name, self.oidset_name,
                self.timestamp)

    def __iter__(self):
        return self.results.__iter__()

    def pickle(self):
        """Produce a pickle which represents this ``PollResult``."""
        return pickle.dumps(self)

    def json(self):
        return json.dumps(dict(
            oidset_name=self.oidset_name,
            device_name=self.device_name,
            oid_name=self.oid_name,
            timestamp=self.timestamp,
            data=self.data,
            metadata=self.metadata))

class PersistQueueEmpty:
    pass

class PollPersister(object):
    """A PollPersister implements a storage method for PollResults."""
    STATS_INTERVAL = 60

    def __init__(self, config, qname, persistq):
        self.log = get_logger("espersistd.%s" % qname)
        self.config = config
        self.qname = qname
        self.running = False
        self.sleeping = False

        if persistq:
            self.persistq = persistq
        else:
            self.persistq = MemcachedPersistQueue(qname, config.espersistd_uri)

        self.data_count = 0
        self.last_stats = time.time()

    def store(self, result):
        pass

    def flush(self):
        """Can be overridden in subclasses if one wishes to perform
        some maintenance during a sleep state."""
        pass

    def stop(self, x, y):
        self.log.debug("stop")
        self.running = False

    def run(self):
        self.log.debug("run")
        self.running = True
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

        if self.config.profile_persister:
            pr = cProfile.Profile()
            pr.enable()

        while self.running:
            try:
                task = self.persistq.get()
            except PersistQueueEmpty:
                break

            # XXX(jdugan): task can be None for two reasons here:
            # 1. there was no result
            # 2. the result was None
            # this means that Nones are consumed very slowly, this should be
            # revisited.

            if task:
                self.store(task)
                self.data_count += len(task.data)
                now = time.time()
                if now > self.last_stats + self.STATS_INTERVAL:
                    self.log.info("%d records written, %f records/sec" % \
                            (self.data_count,
                                float(self.data_count) / self.STATS_INTERVAL))
                    self.data_count = 0
                    self.last_stats = now
                del task
                self.sleeping = False
            else:
                if not self.sleeping:
                    self.flush()
                    self.sleeping = True
                    if self.config.debug:
                        django.db.reset_queries()
                time.sleep(PERSIST_SLEEP_TIME)

        if self.config.profile_persister:
            pr.disable()
            pfile = '{0}-{1}.prof'.format(self.qname, time.time())
            ppath = self.config.traceback_dir + '/' + pfile
            fh = open(ppath, 'a')
            sortby = 'cumulative'
            pstats.Stats(pr, stream=fh).strip_dirs().sort_stats(sortby).print_stats()
            fh.close()


class StreamingPollPersister(PollPersister):
    """A StreamingPollPersister stores PollResults to a log file.

    ``conf.streaming_log_dir``
        Specifies the path name of the log file.

    """
    def __init__(self, config, q):
        PollPersister.__init__(self, config, q)

        self.filename = None
        self.fd = None

    def _rotate_file(self, dst):
        if self.fd:
            self.fd.close()

        self.filename = dst
        self.fd = open(os.path.join(self.config.streaming_log_dir,
            self.filename), "a")

    def store(self, result):
        dst = time.strftime("%Y%m%d_%H", time.gmtime(result.timestamp))
        if dst != self.filename:
            self._rotate_file(dst)

        self.fd.write(result.json())
        self.fd.write("\n\n")
        self.log.debug("stored %s %s %s to streaming log" % (result.oidset_name,
            result.oid_name, result.device_name))

if tsdb:
    class TSDBPollPersister(PollPersister):
        """Given a ``PollResult`` write the data to a TSDB.

        The TSDBWriter will use ``tsdb_root`` in ``config`` as the TSDB instance to
        write to.

        The ``data`` member of the PollResult must be a list of (name,value)
        pairs.  The ``metadata`` member of PollResult must contain the following
        keys::

            ``tsdb_flags``
                TSDB flags to be used

        """

        def __init__(self, config, qname, persistq):
            PollPersister.__init__(self, config, qname, persistq)

            self.tsdb = tsdb.TSDB(self.config.tsdb_root)

            self.oidsets = {}
            self.poller_args = {}
            self.oids = {}
            self.oid_type_map = {}

            oidsets = OIDSet.objects.all()

            for oidset in oidsets:
                self.oidsets[oidset.name] = oidset
                d = {}
                if oidset.poller_args:
                    for arg in oidset.poller_args.split():
                        (k, v) = arg.split('=')
                        d[k] = v
                    self.poller_args[oidset.name] = d

                for oid in oidset.oids.all():
                    self.oids[oid.name] = oid
                    try:
                        self.oid_type_map[oid.name] = eval("tsdb.row.%s" % \
                                oid.oid_type.name)
                    except AttributeError:
                        self.log.warning(
                                "warning don't have a TSDBRow for %s in %s" %
                                (oid.oid_type.name, oidset.name))

        def store(self, result):
            oidset = self.oidsets[result.oidset_name]
            set_name = self.poller_args[oidset.name].get('set_name', oidset.name)
            basename = os.path.join(result.device_name, set_name)
            oid = self.oids[result.oid_name]
            flags = result.metadata['tsdb_flags']

            var_type = self.oid_type_map[oid.name]

            t0 = time.time()
            nvar = 0
            
            for var, val in result.data:
                if set_name == "SparkySet": # This is pure hack. A new TSDB row type should be created for floats
                    val = float(val) * 100
                nvar += 1

                var_name = os.path.join(basename, *map(remove_metachars, var))

                try:
                    tsdb_var = self.tsdb.get_var(var_name)
                except tsdb.TSDBVarDoesNotExistError:
                    tsdb_var = self._create_var(var_type, var_name, oidset, oid)
                except tsdb.InvalidMetaData:
                    tsdb_var = self._repair_var_metadata(var_type, var_name,
                            oidset, oid)
                    continue  # XXX(jdugan): remove this once repair actually works

                tsdb_var.insert(var_type(result.timestamp, flags, val))

                if oid.aggregate:
                    # XXX:refactor uptime should be handled better
                    uptime_name = os.path.join(basename, 'sysUpTime')
                    try:
                        self._aggregate(tsdb_var, var_name, result.timestamp,
                                uptime_name, oidset)
                    except TSDBError, e:
                        self.log.error("Error aggregating: %s %s: %s" %
                                (result.device_name, result.oidset_name, str(e)))

            self.log.debug("stored %d vars in %f seconds: %s" % (nvar,
                time.time() - t0, result))

        def _create_var(self, var_type, var, oidset, oid):
            self.log.debug("creating TSDBVar: %s" % str(var))
            chunk_mapper = eval(self.poller_args[oidset.name]['chunk_mapper'])

            tsdb_var = self.tsdb.add_var(var, var_type,
                    oidset.frequency, chunk_mapper)

            if oid.aggregate:
                self._create_aggs(tsdb_var, oidset)

            tsdb_var.flush()

            return tsdb_var

        def _create_agg(self, tsdb_var, oidset, period):
            chunk_mapper = eval(self.poller_args[oidset.name]['chunk_mapper'])
            if period == oidset.frequency:
                aggs = ['average', 'delta']
            else:
                aggs = ['average', 'delta', 'min', 'max']

            try:
                tsdb_var.add_aggregate(str(period), chunk_mapper, aggs)
            except Exception, e:
                self.log.error("Couldn't create aggregate %s" % (e))

        def _create_aggs(self, tsdb_var, oidset):
            self._create_agg(tsdb_var, oidset, oidset.frequency)

            if 'aggregates' in self.poller_args[oidset.name]:
                aggregates = self.poller_args[oidset.name]['aggregates'].split(',')
                for agg in aggregates:
                    self._create_agg(tsdb_var, oidset, int(agg))

        def _repair_var_metadata(self, var_type, var, oidset, oid):
            self.log.error("var needs repair, skipping: %s" % var)
            #chunk_mapper = eval(self.poller_args[oidset.name]['chunk_mapper'])

        def _aggregate(self, tsdb_var, var_name, timestamp, uptime_name, oidset):
            try:
                uptime = self.tsdb.get_var(uptime_name)
            except TSDBVarDoesNotExistError:
                # XXX this is killing the logger in testing revisit
                #self.log.warning("unable to get uptime for %s" % var_name)
                uptime = None

            # XXX(jdugan): revisit min_last_update
            min_last_update = timestamp - oidset.frequency * 40

            def log_bad(ancestor, agg, rate, prev, curr):
                self.log.debug("bad data for %s at %d: %f" % (ancestor.path,
                    curr.timestamp, rate))

            def update_agg():
                tsdb_var.update_aggregate(str(oidset.frequency),
                    uptime_var=uptime,
                    min_last_update=min_last_update,
                    # XXX(jdugan): should compare to ifHighSpeed?  this is BAD:
                    max_rate=int(110e9),
                    max_rate_callback=log_bad)

            try:
                update_agg()
            except TSDBAggregateDoesNotExistError:
                # XXX(jdugan): this needs to be reworked when we update all aggs
                self.log.error("creating missing aggregate for %s" % var_name)
                self._create_agg(tsdb_var, oidset, oidset.frequency)
                tsdb_var.flush()
                update_agg()
            except InvalidMetaData:
                self.log.error("bad metadata for %s" % var_name)


class CassandraPollPersister(PollPersister):
    """Given a ``PollResult`` write the data to a Cassandra backend.

    The ``data`` member of the PollResult must be a list of (name,value)
    pairs.  The ``metadata`` member of PollResult must contain the following
    keys::

        ``tsdb_flags``
            TSDB flags to be used

    """

    def __init__(self, config, qname, persistq):
        PollPersister.__init__(self, config, qname, persistq)
        # The clear on testing arg - set in the config file if the
        # testing env var is set will result in the target keyspace
        # and all of its data being deleted and rebuilt.
        self.log.debug("connecting to cassandra")
        self.db = CASSANDRA_DB(config, qname=qname)
        self.log.debug("connected to cassandra")

        self.ns = "snmp"

        self.oidsets = {}
        self.poller_args = {}
        self.oids = {}

        oidsets = OIDSet.objects.all()

        for oidset in oidsets:
            self.oidsets[oidset.name] = oidset
            d = {}
            if oidset.poller_args:
                for arg in oidset.poller_args.split():
                    (k, v) = arg.split('=')
                    d[k] = v
                self.poller_args[oidset.name] = d

            for oid in oidset.oids.all():
                self.oids[oid.name] = oid

    def flush(self):
        self.log.debug('flush state called.')
        try:
            self.db.flush()
        except MaximumRetryException:
            self.log.warn("flush failed. MaximumRetryException")

    def store(self, result):
        oidset = self.oidsets[result.oidset_name]
        set_name = self.poller_args[oidset.name].get('set_name', oidset.name)
        basepath = [self.ns, result.device_name, set_name]
        oid = self.oids[result.oid_name]
        
        t0 = time.time()
        nvar = 0

        for var, val in result.data:
            if set_name == "SparkySet": # This is pure hack. A new row type should be created for floats
                val = float(val) * 100
            nvar += 1
            
            var_path = basepath + var

            # This shouldn't happen.
            if val is None:
                self.log.error('Got a None value for %s' % (":".join(var_path)))
                continue
                
            # Create data encapsulation object (defined in cassandra.py 
            # module) and store the raw input.

            raw_data = RawRateData(path=var_path, ts=result.timestamp * 1000,
                    val=val, freq=oidset.frequency_ms)

            self.db.set_raw_data(raw_data, ttl=oidset.ttl)

            # Generate aggregations if apropos.
            if oid.aggregate:
                delta_v = self.aggregate_base_rate(raw_data)
                # XXX: not implemented
                #uptime_name = os.path.join(basename, 'sysUpTime')
                
                if delta_v != None: # a value of zero is ok
                    # We got a good delta back - generate rollups.
                    # Just swap the delta into the raw data object.
                    raw_data.val = delta_v
                    self.generate_aggregations(raw_data, oidset.aggregates)
            else:
                pass

        self.log.debug("stored %d vars in %f seconds: %s" % (nvar,
            time.time() - t0, result))

    def aggregate_base_rate(self, data):
        """
        Given incoming data that is meant for aggregation, generate and 
        store the base rate deltas, update the metadata cache, and if a valid 
        delta (delta_v) is generated, return to calling code to generate
        higher-level rollup aggregations.
        
        The data arg passed in is a RawData encapsulation object as
        defined in the cassandra.py module.  
        
        All of this logic is copied/adapted from the TSDB aggregator.py
        module.
        """

        metadata = self.db.get_metadata(data)
        last_update = metadata.ts_to_jstime('last_update')

        if data.min_last_update and data.min_last_update > last_update:
            last_update = data.min_last_update

        min_ts = metadata.ts_to_jstime('min_ts')

        if min_ts > last_update:
            last_update = min_ts
            metatdata.last_update = last_update

        # This mimics logic in the tsdb persister - skip any further 
        # processing of the rate aggregate if this is the first value

        if data.val == metadata.last_val and \
            data.ts == metadata.last_update:
            return

        last_data_ts = metadata.ts_to_jstime('last_update')
        curr_data_ts = data.ts_to_jstime()

        # We've retrieved valid previous vals/ts from metadata, so calculate
        # the value and time delta, and the fractional slots that the data
        # will (usually) be split between.
        delta_t = curr_data_ts - last_data_ts
        delta_v = data.val - metadata.last_val

        rate = float(delta_v) / float(delta_t)
        # XXX(jdugan): should compare to ifHighSpeed?  this is BAD:
        max_rate = int(110e9)

        # Reality check the current rate and make sure the delta is
        # equal to or greater than zero.  Log errors but still update
        # the metadata cache with the most recently seen raw value/ts 
        # then stop processing.
        if rate > max_rate:
            self.log.error('max_rate_exceeded - %s - %s - %s' \
                % (rate, metadata.last_val, data.val))
            metadata.refresh_from_raw(data)
            return

        if delta_v < 0:
            self.log.error('delta_v < 0: %s vals: %s - %s path: %s' % \
                (delta_v,data.val,metadata.last_val,data.get_meta_key()))
            metadata.refresh_from_raw(data)
            self.db.update_metadata(data.get_meta_key(), metadata)
            return
            
        # This re-implements old "hearbeat" logic.  If the current time
        # delta is greater than HEARTBEAT_FREQ_MULTIPLIER (3), write
        # zero-value non-valid bins in the gap.  These MAY be updated
        # later with valid values or backfill.  Then update only
        # the current bin, update metadata with current slot info
        # and return the delta.
        if delta_t > data.freq * HEARTBEAT_FREQ_MULTIPLIER:
            prev_slot = last_data_ts - (last_data_ts % data.freq)
            curr_slot = curr_data_ts - (curr_data_ts % data.freq)

            self.log.warning(
              'gap exceeds heartbeat for {0} from {1}({2}) to {3}({4})'.format(
                    data.path,
                    time.ctime(last_data_ts/1000),
                    last_data_ts,
                    time.ctime(curr_data_ts/1000),
                    curr_data_ts)
            )

            curr_frac = int(delta_v * ((curr_data_ts - curr_slot)/float(delta_t)))
            # Update only the "current" bin and return.
            curr_bin = BaseRateBin(ts=curr_slot, freq=data.freq, val=curr_frac,
                path=data.path)
            self.db.update_rate_bin(curr_bin)
            
            metadata.refresh_from_raw(data)
            self.db.update_metadata(data.get_meta_key(), metadata)

            return


        updates = fit_to_bins(data.freq, last_data_ts, metadata.last_val,
                curr_data_ts, data.val)
        # Now, write the new valid data between the appropriate bins.

        for bin_name, val in updates.iteritems():
            update_bin = BaseRateBin(ts=bin_name, freq=data.freq, val=val,
                path=data.path)
            self.db.update_rate_bin(update_bin)

        # Gotten to the final success condition, so update the metadata
        # cache with values from the current data input and return the 
        # valid delta to the calling code.
        metadata.refresh_from_raw(data)
        self.db.update_metadata(data.get_meta_key(), metadata)
        
        return delta_v

    def _agg_timestamp(self, data, freq):
        """
        Utility method to generate the 'compressed' timestamp for an higher-level 
        aggregation bin.  
        
        The data arg is a data encapsulation object.
        
        The freq arg is the frequency of the desired aggregation to be written 
        to (ie: 5 mins, hourly, etc) in seconds.
        """
        return datetime.datetime.utcfromtimestamp((data.ts_to_unixtime() / freq) * freq)

    def generate_aggregations(self, data, aggregate_freqs):
        """
        Given a data encapsulation object that has been updated with the 
        current delta, iterate through the frequencies in oidset.aggregates
        and generate the appropriate higher level aggregations.
        
        The 'rate aggregations' are the summed deltas and the associated 
        counts.  The 'stat aggregations' are the min/max values.  These 
        are being writtent to two different column families due to schema
        constraints.
        
        Since the stat aggregations are read from/not just written to, 
        track if a new value has been generated (min/max will only be updated
        periodically), and if so, explicitly flush the stat_agg batch.
        """
        stat_updated = False

        for freq in aggregate_freqs:
            self.db.update_rate_aggregation(data, self._agg_timestamp(data, freq), freq*1000)
            updated = self.db.update_stat_aggregation(data, 
                                        self._agg_timestamp(data, freq), freq*1000)
            if updated: stat_updated = True
                                
        if stat_updated:
            self.db.stat_agg.send()

    def stop(self, x, y):
        self.log.debug("flushing and stopping cassandra poll persister")
        self.db.flush()
        self.running = False
            
        

class HistoryTablePersister(PollPersister):
    """Provides common methods for table histories."""

    def update_db(self):
        """Compare the database to the poll results and update.

        This assumes that the database object has a begin_time and end_time
        and that self.new_data has the dictionary representing the new data
        and that self.old_data contains the database objects representing the
        old data.  It uses _new_row_from_dict() to create a new object when
        needed."""

        adds = 0
        changes = 0
        deletes = 0

        # iterate through what is currently in the database
        for old in self.old_data:
            # there is an entry in the new data: has anything changed?
            key = getattr(old, self.key)
            if key in self.new_data:
                new = self.new_data[key]
                attrs = new.keys()
                attrs.remove(self.key)
                changed = False

                for attr in attrs:
                    if not hasattr(old, attr):
                        self.log.error("Field " + attr + " is not contained in the object: %s. Adding it." % str(old))
                        changed = True
                        break

                    if getattr(old, attr) != new[attr]:
                        changed = True
                        break

                if changed:
                    old.end_time = now()
                    old.save()
                    new_row = self._new_row_from_obj(new)
                    new_row.save()
                    changes += 1

                del self.new_data[key]
            # no entry in self.new_data: interface is gone, update db
            else:
                old.end_time = now()
                old.save()
                deletes += 1

        # anything left in self.new_data is something new
        for new in self.new_data:
            new_row = self._new_row_from_obj(self.new_data[new])
            new_row.save()
            adds += 1

        return (adds, changes, deletes)


class IfRefPollPersister(HistoryTablePersister):
    int_oids = ('ifSpeed', 'ifHighSpeed', 'ifMtu', 'ifType',
            'ifOperStatus', 'ifAdminStatus')

    def store(self, result):
        t0 = time.time()
        self.data = result.data

        self.device = Device.objects.active().get(name=result.device_name)
        self.old_data = IfRef.objects.active().filter(device=self.device)

        self.new_data = self._build_objs()
        nvar = len(self.new_data)
        self.key = 'ifName'

        adds, changes, deletes = self.update_db()

        self.log.debug("processed %d vars [%d/%d/%d] in %f seconds: %s" % (
            nvar, adds, changes, deletes, time.time() - t0, result))

    def _new_row_from_obj(self, obj):
        obj['device'] = self.device
        obj['begin_time'] = now()
        obj['end_time'] = max_datetime
        return IfRef(**obj)

    def _build_objs(self):
        ifref_objs = {}
        ifIndex_map = {}

        for name, val in self.data['ifName']:
            foo, ifIndex = name.split('.')
            ifIndex = int(ifIndex)
            ifName = val
            ifIndex_map[ifIndex] = ifName
            ifref_objs[ifName] = dict(ifName=ifName, ifIndex=ifIndex)

        for name, val in self.data['ipAdEntIfIndex']:
            foo, ipAddr = name.split('.', 1)
            ifref_objs[ifIndex_map[val]]['ipAddr'] = ipAddr

        remaining_oids = self.data.keys()
        remaining_oids.remove('ifName')
        remaining_oids.remove('ipAdEntIfIndex')

        for oid in remaining_oids:
            for name, val in self.data[oid]:
                if oid in self.int_oids:
                    val = int(val)
                foo, ifIndex = name.split('.')
                ifIndex = int(ifIndex)
                ifref_objs[ifIndex_map[ifIndex]][oid] = val

        return ifref_objs

class ALUSAPRefPersister(HistoryTablePersister):
    int_oids = ('sapIngressQosPolicyId', 'sapEgressQosPolicyId')

    def store(self, result):
        self.data = result.data
        t0 = time.time()

        self.device = Device.objects.active().get(name=result.device_name)
        self.old_data = ALUSAPRef.objects.active().filter(device=self.device)

        self.new_data = self._build_objs()
        nvar = len(self.new_data)
        self.key = 'name'

        adds, changes, deletes = self.update_db()

        self.log.debug("processed %d vars [%d/%d/%d] in %f seconds: %s" % (
            nvar, adds, changes, deletes, time.time() - t0, result))

    def _new_row_from_obj(self, obj):
        obj['device'] = self.device
        obj['begin_time'] = now()
        obj['end_time'] = max_datetime

        return ALUSAPRef(**obj)

    def _build_objs(self):
        objs = {}

        for oid, entries in self.data.iteritems():
            for k, val in entries:
                name = build_alu_sap_name(k)

                if oid in self.int_oids:
                    val = int(val)

                if not name in objs:
                    objs[name] = dict(name=name)
                    objs[name]['name'] = name

                o = objs[name]
                o[oid] = val

        return objs

class LSPOpStatusPersister(HistoryTablePersister):
    def __init__(self, config, qname):
        HistoryTablePersister.__init__(self, config, qname)

    def store(self, result):
        self.lsp_data = result.data
        t0 = time.time()

        self.device = Device.objects.active().get(name=result.device_name)
        self.old_data = LSPOpStatus.objects.active().filter(device=self.device)

        self.new_data = self._build_objs()
        nvar = len(self.new_data)
        self.key = 'name'

        adds, changes, deletes = self.update_db()

        self.log.debug("processed %d vars [%d/%d/%d] in %f seconds: %s" % (
            nvar, adds, changes, deletes, time.time() - t0, result))

    def _new_row_from_obj(self, obj):
        obj['device'] = self.device
        obj['begin_time'] = now()
        obj['end_time'] = max_datetime

        return LSPOpStatus(**obj)

    def _build_objs(self):
        lsp_objs = {}

        for k, entries in self.lsp_data.iteritems():
            for name, val in entries:
                name = name.split('.')[-1].replace("'", "")

                if not name in lsp_objs:
                    lsp_objs[name] = dict(name=name)

                o = lsp_objs[name]
                if k == 'mplsLspInfoState':
                    o[k] = int(val)
                else:
                    o[k] = val

        return lsp_objs

class InfIfRefPollPersister(IfRefPollPersister):
    """Emulate a IfRef for an Infinera.

    This is a kludge, but it keeps other things relatively simple.

    ifAlias is called gigeClientCtpPmRealCktId
    ifSpeed and ifHighSpeed are apparently not available
    ipAdEntIfIndex doesn't make sense because this is not a layer3 device."""

    def store(self, result):
        keep = []
        result.data['ifAlias'] = []
        result.data['ifSpeed'] = []
        result.data['ifHighSpeed'] = []
        result.data['ipAdEntIfIndex'] = []

        ifalias = {}
        for k, v in result.data['gigeClientCtpPmRealCktId']:
            _, ifidx = k.split('.', 1)
            ifalias[ifidx] = v

        for k, v in result.data['ifName']:
            if v.startswith('GIGECLIENTCTP'):
                _, ifname = v.split('=', 1)
                keep.append((k, ifname))
                _, ifidx = k.split('.', 1)
                result.data['ifAlias'].append(
                            ('ifAlias.' + ifidx, ifalias.get(ifidx, '')))
                for x in ('ifSpeed', 'ifHighSpeed'):
                    result.data[x].append(
                            ('%s.%s' % (x, ifidx), 0))

        result.data['ifName'] = keep
        del result.data['gigeClientCtpPmRealCktId']

        IfRefPollPersister.store(self, result)

class SentryOutletRefPollPersister(HistoryTablePersister):
    """Save information about outlets for a Sentry PDU."""
    int_oids = ('outletStatus', 'outletControlState')

    def store(self, result):
        self.data = result.data
        t0 = time.time()

        self.device = Device.objects.active().get(name=result.device_name)
        self.old_data = OutletRef.objects.active().filter(device=self.device)

        self.new_data = self._build_objs()
        nvar = len(self.new_data)
        self.key = 'outletID'

        adds, changes, deletes = self.update_db()

        self.log.debug("processed %d vars [%d/%d/%d] in %f seconds: %s" % (
            nvar, adds, changes, deletes, time.time() - t0, result))

    def _new_row_from_obj(self, obj):
        obj['device'] = self.device
        obj['begin_time'] = now()
        obj['end_time'] = max_datetime

        return OutletRef(**obj)

    def _build_objs(self):
        objs = {}
        outletID_map = {}

        for k, val in self.data['outletID']:
            var, index = k.split('.', 1)
            outletID_map[index] = val
            objs[val] = dict(outletID=val)

        for oid, entries in self.data.iteritems():
            if oid == 'outletID':
                continue

            for k, val in entries:
                _, index = k.split('.', 1)
                o = objs[outletID_map[index]]

                if oid in self.int_oids:
                    val = int(val)

                o[oid] = val

        return objs

class PersistQueue(object):
    """Abstract base class for a persistence queue."""
    def __init__(self, qname):
        self.qname = qname

    def get(self, block=False):
        pass

    def put(self, val):
        pass

    def serialize(self, val):
        # return pickle.dumps(val)
        try:
            return val.json() # .dumps() is being called in the PollResult method
        except Exception as e:
            m = 'Poll Result {0} could not be serialized: {1}'.format(val, e)
            if hasattr(self, 'log'):
                self.log.error(m)
            # Not sure if the logger for this class is working and 
            # we want an error in other cases anyways.
            print >>sys.stderr, m
            return None

    def deserialize(self, val):
        # return pickle.loads(val)
        return json.loads(val)

class JsonSerializer(object):
    """This is passed to memcache.Client() to replace default use of 
    pickle to de/serialize."""
    def __init__(self, file, protocol=None):
        super(JsonSerializer, self).__init__()
        self.file = file
        self.protocol = protocol

    def dump(self, value):
        json.dump(value, self.file)

    def load(self, value):
        json.load(self.file)

class MemcachedPersistQueue(PersistQueue):
    """A simple queue based on memcached.

    Inspired by:

    http://code.google.com/p/memcached/wiki/FAQ#Using_Memcached_as_a_simple_message_queue
    http://github.com/coderrr/memcache_queue/tree/master
    http://bitbucket.org/epoz/python-memcache-queue/overview/

    Code is very similar to python-memcache-queue but tailored to our needs.
    """

    PREFIX = '_mcpq_'

    def __init__(self, qname, memcached_uri):
        super(MemcachedPersistQueue, self).__init__(qname)

        self.log = get_logger("MemcachedPersistQueue_%s" % self.qname)

        # self.mc = memcache.Client([memcached_uri])
        self.mc = memcache.Client([memcached_uri],
            pickler=JsonSerializer, unpickler=JsonSerializer)

        self.last_added = '%s_%s_last_added' % (self.PREFIX, self.qname)
        la = self.mc.get(self.last_added)
        if not la:
            self.mc.set(self.last_added, 0)

        self.last_read = '%s_%s_last_read' % (self.PREFIX, self.qname)
        lr = self.mc.get(self.last_read)
        if not lr:
            self.mc.set(self.last_read, 0)

    def __str__(self):
        la = self.mc.get(self.last_added)
        lr = self.mc.get(self.last_read)
        return '<MemcachedPersistQueue: %s last_added: %d, last_read: %d>' \
                % (self.qname, la, lr)

    def put(self, val):
        ser = self.serialize(val)
        if ser:
            qid = self.mc.incr(self.last_added)
            k = '%s_%s_%d' % (self.PREFIX, self.qname, qid)
            if not self.mc.set(k, ser):
                self.log.error("memcache 'set' failed! Polling data lost!")
        else:
            self.log.error("failed to serialize: %s" % str(val))

    def get(self, block=False):
        if len(self) <= 0:
            return None

        errors = 0

        qid = self.mc.incr(self.last_read)
        while qid <= self.mc.get(self.last_added):
            k = '%s_%s_%d' % (self.PREFIX, self.qname, qid)
            val = self.mc.get(k)
            if val:
                self.mc.delete(k)
                if errors:
                    self.log.error("missing data: %d items missing (qids %d-%d)" %
                            (errors, qid-errors, qid-1))
                # return self.deserialize(val)
                return PollResult(**self.deserialize(val))

            errors += 1

            qid = self.mc.incr(self.last_read)

    def __len__(self):
        n = self.mc.get(self.last_added) - self.mc.get(self.last_read)
        if n < 0:
            n = 0
        return n

    def reset(self):
        self.mc.set(self.last_added, 0)
        self.mc.set(self.last_read, 0)


class PersistClient(object):
    def __init__(self, name, config):
        self.config = config
        self.sinks = []
        self.log = get_logger("espersist.client")

        if not self.config.espoll_persist_uri:
            self.log.warning(
                "espoll_persist_uri not defined: all data will be discarded")
            return

        for uri in config.espoll_persist_uri:
            (kind, kind_uri) = uri.split(':', 1)
            sink = eval('%s(name, config, "%s")' % (kind, kind_uri))
            self.sinks.append(sink)

    def put(self, result):
        for sink in self.sinks:
            sink.put(result)


class MultiWorkerQueue(object):
    def __init__(self, qprefix, qtype, uri, num_workers):
        self.qprefix = qprefix
        self.qtype = qtype
        self.num_workers = num_workers
        self.queues = {}
        self.worker_map = {}
        self.log = get_logger('MultiWorkerQueue')
        self.worker_load = []

        for i in range(1, num_workers + 1):
            name = "%s_%d" % (qprefix, i)
            self.queues[name] = qtype(name, uri)
            self.worker_load.append([i, 0])

    def get_worker(self, result):
        k = ":".join((result.oidset_name, result.device_name))
        try:
            w = self.worker_map[k]
        except KeyError:
            work_size = len(result.data)
            w = self.worker_load[0][0]
            self.worker_map[k] = w

            self.worker_load[0][1] += work_size

            self.log.debug("worker assigned: %s %d load=%d" % (k, w,
                self.worker_load[0][1]))

            self.worker_load.sort(key=lambda x: x[1])

        return '%s_%d' % (self.qprefix, w)

    def put(self, result):
        workerqname = self.get_worker(result)
        workerq = self.queues[workerqname]
        workerq.put(result)


class MemcachedPersistHandler(object):
    def __init__(self, name, config, uri):
        self.queues = {}
        self.config = config
        self.uri = uri
        self.log = get_logger(name)

        for qname in config.persist_queues:
            num_workers = self.config.persist_queues[qname][1]
            if num_workers > 1:
                self.queues[qname] = MultiWorkerQueue(qname,
                        MemcachedPersistQueue, uri, num_workers)
            else:
                self.queues[qname] = MemcachedPersistQueue(qname, uri)

    def put(self, result):
        try:
            qnames = self.config.persist_map[result.oidset_name.lower()]
        except KeyError:
            self.log.error("unknown oidset: %s" % result.oidset_name)
            return

        for qname in qnames:
            try:
                q = self.queues[qname]
            except KeyError:
                self.log.error("unknown queue: %s" % (qname,))

            q.put(result)


def do_profile(func_name, myglobals, mylocals):
    import cProfile
    import pstats
    prof = cProfile.Profile()

    def print_stats(prof):
        stats = pstats.Stats(prof)
        #stats.sort_stats("time")  # Or cumulative
        #stats.print_stats()  # 80 = how many to print
        # The rest is optional.
        #stats.print_callees()
        #stats.print_callers()
        stats.dump_stats("/tmp/persists-profile.%d" % os.getpid())
    try:
        prof = prof.runctx(func_name, myglobals, mylocals)
    except Exception, e:
        print_stats(prof)
        raise e
    print_stats(prof)


class QueueStats:
    prefix = '_mcpq_'

    def __init__(self, mc, qname):
        self.mc = mc
        self.qname = qname
        self.last_read = [0, 0]
        self.last_added = [0, 0]
        self.warn = False

    def update_stats(self):
        for k in ('last_read', 'last_added'):
            kk = '%s_%s_%s' % (self.prefix, self.qname, k)
            v = self.mc.get(kk)
            l = getattr(self, k)
            if v:
                l.pop()
                l.insert(0, int(v))
            elif not self.warn:
                print >>sys.stderr, \
                        "warning: no stats, no work queue %s in memcache" \
                                % (self.qname, )
                self.warn = True
                break

    def get_stats(self):
        pending = self.last_added[0] - self.last_read[0]
        new = self.last_added[0] - self.last_added[1]
        done = self.last_read[0] - self.last_read[1]
        delta = new - done
        return (self.qname,
                pending,
                new,
                done,
                delta,
                self.last_added[0])


def stats(name, config, opts):
    stats = {}
    mc = memcache.Client(['127.0.0.1:11211'])

    for qname, qinfo in config.persist_queues.iteritems():
        (qclass, nworkers) = qinfo
        if nworkers == 1:
                stats[qname] = QueueStats(mc, qname)
                stats[qname].update_stats()
        else:
            for i in range(1, nworkers + 1):
                k = "%s_%d" % (qname, i)
                stats[k] = QueueStats(mc, k)
                stats[k].update_stats()

    keys = stats.keys()
    keys.sort()
    while True:
        total = [0,0,0,0]
        print "%20s %8s %8s %8s %8s %14s" % (
                "queue", "pending", "new", "done", "delta", "max")
        for k in keys:
            stats[k].update_stats()
            vals = stats[k].get_stats()
            print "%20s % 8d % 8d % 8d % 8d % 14d" % vals
            total = map(sum, zip(total, vals[1:-1]))
        total.insert(0, "TOTAL")
        print "%20s % 8d % 8d % 8d % 8d" % tuple(total)
        print ""
        time.sleep(5)


def worker(name, config, opts):
    if not opts.debug:
        exc_handler = setup_exc_handler(name, config)
        exc_handler.install()

    os.umask(0022)

    (qclass, nworkers) = config.persist_queues[opts.qname]
    if nworkers > 1:
        name += '_%s' % opts.number
        opts.qname += '_%s' % opts.number

    init_logging("espersistd." + opts.qname, config.syslog_facility, level=config.syslog_priority,
            debug=opts.debug)

    setproctitle(name)
    klass = eval(qclass)
    worker = klass(config, opts.qname, persistq=None)

    worker.run()
    # do_profile("worker.run()", globals(), locals())


class PersistManager(object):
    def __init__(self, name, config, opts):
        self.name = name
        self.config = config
        self.opts = opts
        self.runing = False

        self.processes = {}

        if tsdb:
            if config.tsdb_root and not os.path.isdir(config.tsdb_root):
                try:
                    tsdb.TSDB.create(config.tsdb_root)
                except Exception, e:
                    print >>sys.stderr, "unable to create TSDB root: %s: %s" % (config.tsdb_root, str(e))

        init_logging(name, config.syslog_facility, level=config.syslog_priority,
            debug=opts.debug)

        self.log = get_logger(name)
        # save the location of the calling script for later use
        # (os.path.abspath uses current directory and daemonize does a cd /)
        self.caller_path = os.path.abspath(__main__.__file__)

        if not self.opts.debug:
            exc_handler = setup_exc_handler(name, config)
            exc_handler.install()

            daemonize(name, config.pid_dir,
                    log_stdout_stderr=config.syslog_facility)

        os.umask(0022)

        setproctitle(name)
        signal.signal(signal.SIGINT, self.stop)
        signal.signal(signal.SIGTERM, self.stop)

    def start_all_children(self):
        for qname, qinfo in self.config.persist_queues.iteritems():
            (qclass, nworkers) = qinfo
            for i in range(1, nworkers + 1):
                self.start_child(qname, qclass, i)

    def start_child(self, qname, qclass, index):
        args = [sys.executable, self.caller_path,
                '-r', 'worker',
                '-q', qname,
                '-f', self.opts.config_file]

        if self.config.persist_queues[qname][1] > 1:
            args.extend(['-n', str(index)])

        p = Popen(args, stdout=PIPE, stderr=STDOUT)

        self.processes[p.pid] = (p, qname, qclass, index)

    def run(self):
        self.log.info("starting")
        self.running = True

        self.start_all_children()

        while self.running:
            try:
                pid, status = os.wait()
            except OSError, e:
                if e.errno == errno.EINTR:
                    continue
                else:
                    raise

            p, qname, qclass, index = self.processes[pid]
            del self.processes[pid]
            self.log.error("child died: pid %d, %s_%d" % (pid, qname, index))
            for line in p.stdout.readlines():
                self.log.error("pid %d: %s" % (pid, line))

            self.start_child(qname, qclass, index)

        for pid, pinfo in self.processes.iteritems():
            p, qname, qclass, index = pinfo
            self.log.info("killing pid %d: %s_%d" % (pid, qname, index))

            os.kill(pid, signal.SIGTERM)
            os.waitpid(pid, 0)

        self.log.info("exiting")

    def stop(self, x, y):
        self.log.info("stopping")
        self.running = False


def espersistd():
    """Entry point for espersistd.

    espersistd consists of one PersistenceManager thread and multiple
    worker sub-processes.

    """
    django.setup()

    argv = sys.argv
    oparse = get_opt_parser(default_config_file=get_config_path())
    oparse.add_option("-r", "--role", dest="role", default="manager")
    oparse.add_option("-q", "--queue", dest="qname", default="")
    oparse.add_option("-n", "--number", dest="number", default="")
    (opts, args) = oparse.parse_args(args=argv)

    opts.config_file = os.path.abspath(opts.config_file)

    try:
        config = get_config(opts.config_file, opts)
    except ConfigError, e:
        print >>sys.stderr, e
        sys.exit(1)

    name = "espersistd.%s" % opts.role

    if opts.qname:
        name += ".%s" % opts.qname

    log = get_logger(name)

    if opts.role == 'manager':
        try:
            PersistManager(name, config, opts).run()
        except Exception, e:
            log.error("Problem with manager module: %s" % e, exc_info=True)
            raise
            sys.exit(1)
    elif opts.role == 'worker':
        try:
            worker(name, config, opts)
        except Exception, e:
            log.error("Problem with worker module: %s" % e, exc_info=True)
            raise
            sys.exit(1)
    elif opts.role == 'stats':
        stats(name, config, opts)
    else:
        print >>sys.stderr, "unknown role: %s" % opts.role

def espersistq():
    """Entry point for espersistq.

    """

    argv = sys.argv
    oparse = get_opt_parser(default_config_file=get_config_path())
    (opts, args) = oparse.parse_args(args=argv)

    opts.config_file = os.path.abspath(opts.config_file)

    try:
        config = get_config(opts.config_file, opts)
    except ConfigError, e:
        print >>sys.stderr, e
        sys.exit(1)

    stats("espersistq", config, opts)
