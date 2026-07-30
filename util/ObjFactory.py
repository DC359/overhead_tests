from statsCollector.schedstatCollector import schedstatCollector
from statsCollector.bpfSnapCollector import bpfSnapCollector
from workload.dirtyHarry import dirtyHarry
from workload.fioWorkload import fioWorkload
from workload.iperfWorkload import iperfWorkload  # IPERF_ADDITION
from workload.redisWorkload import redisWorkload
from statsDump.csvDump import csvDump
from statsDump.terminalDump import terminalDump


class ObjFactory:
    _statsCollectorDic = {
        "schedstat": schedstatCollector,
        "bpfsnap": bpfSnapCollector,
    }

    _workloadDic = {
        "dirtyHarry": dirtyHarry,
        "fio": fioWorkload,
        "iperf": iperfWorkload,  # IPERF_ADDITION
        "redis": redisWorkload,
    }

    _statsDumpDic = {
        "csvDump": csvDump,
        "terminalDump": terminalDump,
    }

    @staticmethod
    def getStatsCollectorObj(id):
        if not id in ObjFactory._statsCollectorDic:
            exit(1)
        return ObjFactory._statsCollectorDic[id]()

    @staticmethod
    def getWorkloadObj(id):
        if not id in ObjFactory._workloadDic:
            exit(1)
        return ObjFactory._workloadDic[id]()

    @staticmethod
    def getStatsDumpObj(id):
        if not id in ObjFactory._statsDumpDic:
            exit(1)
        return ObjFactory._statsDumpDic[id]()
