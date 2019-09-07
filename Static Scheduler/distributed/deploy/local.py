from __future__ import print_function, division, absolute_import

import atexit
import logging
import math
import warnings
import weakref

from dask.utils import factors

from .spec import SpecCluster
from ..nanny import Nanny
from ..scheduler import Scheduler
from ..worker import Worker, parse_memory_limit, _ncores

logger = logging.getLogger(__name__)


class LocalCluster(SpecCluster):
    """ Create local Scheduler and Workers

    This creates a "cluster" of a scheduler and workers running on the local
    machine.

    Parameters
    ----------
    n_workers: int
        Number of workers to start
    processes: bool
        Whether to use processes (True) or threads (False).  Defaults to True
    threads_per_worker: int
        Number of threads per each worker
    scheduler_port: int
        Port of the scheduler.  8786 by default, use 0 to choose a random port
    silence_logs: logging level
        Level of logs to print out to stdout.  ``logging.WARN`` by default.
        Use a falsey value like False or None for no change.
    host: string
        Host address on which the scheduler will listen, defaults to only localhost
    ip: string
        Deprecated.  See ``host`` above.
    dashboard_address: str
        Address on which to listen for the Bokeh diagnostics server like
        'localhost:8787' or '0.0.0.0:8787'.  Defaults to ':8787'.
        Set to ``None`` to disable the dashboard.
        Use ':0' for a random port.
    diagnostics_port: int
        Deprecated.  See dashboard_address.
    asynchronous: bool (False by default)
        Set to True if using this cluster within async/await functions or within
        Tornado gen.coroutines.  This should remain False for normal use.
    worker_kwargs: dict
        Extra worker arguments, will be passed to the Worker constructor.
    blocked_handlers: List[str]
        A list of strings specifying a blacklist of handlers to disallow on the Scheduler,
        like ``['feed', 'run_function']``
    service_kwargs: Dict[str, Dict]
        Extra keywords to hand to the running services
    security : Security
    protocol: str (optional)
        Protocol to use like ``tcp://``, ``tls://``, ``inproc://``
        This defaults to sensible choice given other keyword arguments like
        ``processes`` and ``security``
    interface: str (optional)
        Network interface to use.  Defaults to lo/localhost
    worker_class: Worker
        Worker class used to instantiate workers from.
    proxy_and_redis_address: str
        The IP address of both the proxy and the Redis cluster(s)
    proxy_port: int
        The port of the proxy 
    redis_ports: [int]
        List of ports on which there are redis instances listening

    Examples
    --------
    >>> cluster = LocalCluster()  # Create a local cluster with as many workers as cores  # doctest: +SKIP
    >>> cluster  # doctest: +SKIP
    LocalCluster("127.0.0.1:8786", workers=8, ncores=8)

    >>> c = Client(cluster)  # connect to local cluster  # doctest: +SKIP

    Scale the cluster to three workers

    >>> cluster.scale(3)  # doctest: +SKIP

    Pass extra keyword arguments to Bokeh

    >>> LocalCluster(service_kwargs={'bokeh': {'prefix': '/foo'}})  # doctest: +SKIP
    """

    def __init__(
        self,
        n_workers=None,
        threads_per_worker=None,
        processes=True,
        loop=None,
        start=None,
        host=None,
        ip=None,
        scheduler_port=0,
        silence_logs=logging.WARN,
        dashboard_address=":8787",
        worker_dashboard_address=None,
        diagnostics_port=None,
        services=None,
        worker_services=None,
        service_kwargs=None,
        asynchronous=False,
        security=None,
        protocol=None,
        blocked_handlers=None,
        interface=None,
        worker_class=None,
        proxy_address = None,
        proxy_port = None,
        redis_endpoints = [],
        num_lambda_invokers = 16,
        max_task_fanout = 10,
        chunk_large_tasks = False,
        chunk_task_threshold = 50,
        num_chunks_for_large_tasks = None,
        **worker_kwargs
    ):
        if ip is not None:
            warnings.warn("The ip keyword has been moved to host")
            host = ip

        if diagnostics_port is not None:
            warnings.warn(
                "diagnostics_port has been deprecated. "
                "Please use `dashboard_address=` instead"
            )
            dashboard_address = diagnostics_port

        self.status = None
        self.processes = processes

        if protocol is None:
            if host and "://" in host:
                protocol = host.split("://")[0]
            elif security:
                protocol = "tls://"
            elif not self.processes and not scheduler_port:
                protocol = "inproc://"
            else:
                protocol = "tcp://"
        if not protocol.endswith("://"):
            protocol = protocol + "://"

        if host is None and not protocol.startswith("inproc") and not interface:
            host = "127.0.0.1"

        services = services or {}
        worker_services = worker_services or {}
        if n_workers is None and threads_per_worker is None:
            if processes:
                n_workers, threads_per_worker = nprocesses_nthreads(_ncores)
            else:
                n_workers = 1
                threads_per_worker = _ncores
        if n_workers is None and threads_per_worker is not None:
            n_workers = max(1, _ncores // threads_per_worker)
        if n_workers and threads_per_worker is None:
            # Overcommit threads per worker, rather than undercommit
            threads_per_worker = max(1, int(math.ceil(_ncores / n_workers)))
        if n_workers and "memory_limit" not in worker_kwargs:
            worker_kwargs["memory_limit"] = parse_memory_limit("auto", 1, n_workers)

        worker_kwargs.update(
            {
                "ncores": threads_per_worker,
                "services": worker_services,
                "dashboard_address": worker_dashboard_address,
                "interface": interface,
                "protocol": protocol,
                "security": security,
                "silence_logs": silence_logs,
            }
        )

        scheduler = {
            "cls": Scheduler,
            "options": dict(
                host=host,
                services=services,
                service_kwargs=service_kwargs,
                security=security,
                port=scheduler_port,
                interface=interface,
                protocol=protocol,
                dashboard_address=dashboard_address,
                blocked_handlers=blocked_handlers,
                proxy_address = proxy_address,
                proxy_port = proxy_port,
                redis_endpoints = redis_endpoints, 
                num_lambda_invokers = num_lambda_invokers,
                chunk_large_tasks = chunk_large_tasks,
                chunk_task_threshold = chunk_task_threshold,
                max_task_fanout = max_task_fanout,
                num_chunks_for_large_tasks = num_chunks_for_large_tasks
            ),
        }

        worker = {
            "cls": worker_class or (Worker if not processes else Nanny),
            "options": worker_kwargs,
        }

        workers = {i: worker for i in range(n_workers)}

        super(LocalCluster, self).__init__(
            scheduler=scheduler,
            workers=workers,
            worker=worker,
            loop=loop,
            asynchronous=asynchronous,
            silence_logs=silence_logs,
        )
        self.scale(n_workers)


def nprocesses_nthreads(n):
    """
    The default breakdown of processes and threads for a given number of cores

    Parameters
    ----------
    n: int
        Number of available cores

    Examples
    --------
    >>> nprocesses_nthreads(4)
    (4, 1)
    >>> nprocesses_nthreads(32)
    (8, 4)

    Returns
    -------
    nprocesses, nthreads
    """
    if n <= 4:
        processes = n
    else:
        processes = min(f for f in factors(n) if f >= math.sqrt(n))
    threads = n // processes
    return (processes, threads)


clusters_to_close = weakref.WeakSet()


@atexit.register
def close_clusters():
    for cluster in list(clusters_to_close):
        cluster.close(timeout=10)