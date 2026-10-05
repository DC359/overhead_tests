from workloads.dirty_harry import dirtyHarry
from workloads.fio import fioWorkload
from workloads.redis import redisWorkload


class ObjFactory:
    _workloadDic = {
        "dirtyHarry": dirtyHarry,
        "fio": fioWorkload,
        "redis": redisWorkload,
    }

    @staticmethod
    def getWorkloadObj(id):
        if id not in ObjFactory._workloadDic:
            available = list(ObjFactory._workloadDic.keys())
            raise ValueError("Unknown workload '%s'. Available: %s" % (id, available))
        return ObjFactory._workloadDic[id]()
