from statsCollector.schedstatCollector import schedstatCollector
from workload.dirtyHarry import dirtyHarry
from workload.fioWorkload import fioWorkload
from workload.iperfWorkload import iperfWorkload  # IPERF_ADDITION
from statsDump.csvDump import csvDump
from statsDump.terminalDump import terminalDump


class ObjFactory:
    _statsCollectorDic = {
        "schedstat": schedstatCollector,
    }

    _workloadDic = {
        "dirtyHarry": dirtyHarry,
        "fio": fioWorkload,
        "iperf": iperfWorkload,  # IPERF_ADDITION
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
