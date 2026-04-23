import abc


class StatsCollectorTmpl(abc.ABC):

    @abc.abstractmethod
    def setup(self):
        pass

    @abc.abstractmethod
    def runx(self):
        pass

    @abc.abstractmethod
    def exportStats(self):
        pass


class WorkloadTmpl(abc.ABC):

    @abc.abstractmethod
    def setup(self):
        pass

    @abc.abstractmethod
    def runx(self, config):
        pass


class StatsDumpTmpl(abc.ABC):

    @abc.abstractmethod
    def dumpStats(self, title, stats):
        pass
