#!/home/jdugan/sw/bin/thrift -cpp -py -r

cpp_namespace ESSNMP

struct Var {
    1: string name,
    2: byte type_id,
    3: i32 device_id
}

struct OID {
    1: i32 id,
    2: string name,
    3: string storage,
    4: i32 oidtypeid
}

struct OIDSet {
    1: i32 id,
    2: string name,
    3: i32 frequency,
    4: list<OID> oids
}

struct Device {
    1: i32 id,
    2: string name,
    3: i32 begin_time,
    4: i32 end_time,
    5: string community,
    6: list<OIDSet> oidsets
}

enum Grouping {
    Commercial = 1,
    Internal = 2,
    ResearchEducation = 3,
    Education = 4,
    Site = 5
}

struct Counter32 {
    1: i32 flags,
    2: i32 timestamp,
    3: i32 value,
    4: byte version = 1,
    5: byte type_id = 1
}

struct Counter64 {
    1: i32 flags,
    2: i32 timestamp,
    3: i64 value,
    4: byte version = 1,
    5: byte type_id = 1
}

struct Gauge32 {
    1: i32 flags,
    2: i32 timestamp,
    3: i32 value,
    4: byte version = 1,
    5: byte type_id = 1
}

struct VarList {
    1: list<Counter32> counter32,
    2: list<Counter64> counter64,
    3: list<Gauge32> gauge32
}

service ESDB {
    list<string> list_devices(),
    Device get_device(1: string name),
    map<string, Device> get_all_devices(),
    void add_device(1: string name, 2: string begin_time, 3: string end_time),
    void update_device(1: string name, 2: string begin_time, 3: string end_time),
    list<OIDSet> list_device_oidsets(1: Device device),

    list<string> list_oids(),
    OID get_oid(1: string name),
    void add_oid(1: string name, 2: string storage, 3: string oidtype),
    
    list<string> list_oidsets(),
    OIDSet get_oidset(1: string name),
    list<Device> get_oidset_devices(1: OIDSet oidset),

    VarList get_vars_by_grouping(1: Grouping grouping), // should grouping e
    void insert_counter32(list<Var> vars, list<Counter32> values),
    void insert_counter64(list<Var> vars, list<Counter64> values),
    void insert_gauge32(list<Var> vars, list<Gauge32> values),
}
