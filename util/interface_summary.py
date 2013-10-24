#!/usr/bin/env python

"""
Quick one off to query interface data from API.  Starter sketch for 
summary tools.
"""

import datetime
import os
import requests
import sys
import time

from optparse import OptionParser

from esmond.api.client.snmp import ApiConnect, ApiFilters, BulkDataPayload
from esmond.api.client.timeseries import PostRawData, GetRawData

SUMMARY_NS = 'summary'

def main():    
    usage = '%prog [ -U rest url (required) | -i ifDescr pattern | -a alias pattern | -e endpoint -e endpoint (multiple ok) ]'
    parser = OptionParser(usage=usage)
    parser.add_option('-U', '--url', metavar='ESMOND_REST_URL',
            type='string', dest='api_url', 
            help='URL for the REST API (default=%default) - required.',
            default='http://localhost')
    parser.add_option('-i', '--ifdescr', metavar='IFDESCR',
            type='string', dest='ifdescr_pattern', 
            help='Pattern to apply to interface ifdescr search.')
    parser.add_option('-a', '--alias', metavar='ALIAS',
            type='string', dest='alias_pattern', 
            help='Pattern to apply to interface alias search.')
    parser.add_option('-e', '--endpoint', metavar='ENDPOINT',
            dest='endpoint', action='append', default=[],
            help='Endpoint type to query (required) - can specify more than one.')
    parser.add_option('-l', '--last', metavar='LAST',
            type='int', dest='last', default=0,
            help='Last n minutes of data to query - api defaults to 60 if not given.')
    parser.add_option('-v', '--verbose',
                dest='verbose', action='count', default=False,
                help='Verbose output - -v, -vv, etc.')
    options, args = parser.parse_args()
    
    filters = ApiFilters()

    filters.verbose = options.verbose

    if options.last:
        filters.begin_time = int(time.time() - (options.last*60))
    else:
        # Default to one minute ago, then rounded to the nearest 30 
        # second bin.
        time_point = int(time.time() - 60)
        bin_point = time_point - (time_point % 30)
        filters.begin_time = filters.end_time = bin_point

    if not options.ifdescr_pattern and not options.alias_pattern:
        # Don't grab *everthing*.
        print 'Specify an ifdescr or alias filter option.'
        parser.print_help()
        return -1
    elif options.ifdescr_pattern and options.alias_pattern:
        # Keep it simple for now, flesh this out later.
        print 'Specify only one filter option.'
        parser.print_help()
        return -1
    else:
        if options.ifdescr_pattern:
            interface_filters = { 'ifDescr__contains': options.ifdescr_pattern }
        elif options.alias_pattern:
            interface_filters = { 'ifAlias__contains': options.alias_pattern }

    if not options.endpoint:
        print 'No endpoints specified: {0}'.format(valid_endpoints)
        parser.print_help()
        return -1
    else:
        filters.endpoint = options.endpoint
        pass

    conn = ApiConnect(options.api_url, filters)

    data = conn.get_interface_bulk_data(**interface_filters)

    print data

    aggs = {}

    # Aggregate the returned data by timestamp and endpoint alias.
    for row in data.data:
        # do something....
        if options.verbose: print ' *', row
        for data in row.data:
            if options.verbose > 1: print '  *', data
            if not aggs.has_key(data.ts_epoch): aggs[data.ts_epoch] = {}
            if not aggs[data.ts_epoch].has_key(row.endpoint): 
                aggs[data.ts_epoch][row.endpoint] = 0
            if data.val != None:
                aggs[data.ts_epoch][row.endpoint] += data.val
        pass

    # And example of how the summary name is tied to a specific search
    # option.
    summary_type_map = {
        'ifdescr' : {
            'me0.0': 'TotalTrafficMe0.0'
        },
        'ifalias' : {
            'intercloud' : 'TotalTrafficIntercloud'
        }
    }

    summary_name = None

    if interface_filters.get('ifDescr__contains', None):
        summary_name = \
            summary_type_map['ifdescr'].get(interface_filters['ifDescr__contains'], None)
    elif interface_filters.get('ifAlias__contains', None):
        summary_name = \
            summary_type_map['ifalias'].get(interface_filters['ifAlias__contains'], None)
    else:
        print 'Could not find summary type for filter criteria {0}'.format(interface_filters.keys())
        return

    if not summary_name:
        print 'Could not find summary type for search pattern {0}'.format(interface_filters.values())
        return

    bin_steps = aggs.keys()[:]
    bin_steps.sort()

    # Might be searching over a time period, so re-aggregate based on 
    # path so that we only need to do one write per endpoint alias, rather
    # than a write for every data point.

    path_aggregation = {}

    for bin_ts in bin_steps:
        if options.verbose > 1: print bin_ts
        for endpoint in aggs[bin_ts].keys():
            path = (SUMMARY_NS, summary_name, endpoint)
            if not path_aggregation.has_key(path):
                path_aggregation[path] = []
            if options.verbose > 1: print ' *', endpoint, ':', aggs[bin_ts][endpoint], path
            path_aggregation[path].append({'ts': bin_ts*1000, 'val': aggs[bin_ts][endpoint]})

    for k,v in path_aggregation.items():
        args = {
            'api_url': options.api_url, 'path': list(k), 'freq': 30000
        }
        p = PostRawData(**args)
        p.set_payload(v)
        p.send_data()
        if options.verbose:
            print 'verifying write'
            g = GetRawData(**args)
            payload = g.get_data()
            print payload
            for d in payload.data:
                print '  *', d


    pass

if __name__ == '__main__':
    main()