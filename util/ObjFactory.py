from statsCollector.schedstatCollector import schedstatCollector
from statsCollector.bpfSnapCollector import bpfSnapCollector
from workload.dirtyHarry import dirtyHarry
from workload.fioWorkload import fioWorkload
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
        "redis": redisWorkload,
    }

    _statsDumpDic = {
        "csvDump": csvDump,
        "terminalDump": terminalDump,
    }

    @staticmethod
    def getStatsCollectorObj(id):
        if not id in ObjFactory._statsCollectorDic:
            available = list(ObjFactory._statsCollectorDic.keys())
            raise ValueError("Unknown collector '%s'. Available: %s" % (id, available))
        return ObjFactory._statsCollectorDic[id]()

    @staticmethod
    def getWorkloadObj(id):
        if not id in ObjFactory._workloadDic:
            available = list(ObjFactory._workloadDic.keys())
            raise ValueError("Unknown workload '%s'. Available: %s" % (id, available))
        return ObjFactory._workloadDic[id]()

    @staticmethod
    def getStatsDumpObj(id):
        if not id in ObjFactory._statsDumpDic:
            available = list(ObjFactory._statsDumpDic.keys())
            raise ValueError("Unknown stats dump '%s'. Available: %s" % (id, available))
        return ObjFactory._statsDumpDic[id]()
