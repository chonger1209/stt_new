"""Control failures distinguished from transport/model loading timeouts."""


class ServiceLockBusy(RuntimeError):
    pass
