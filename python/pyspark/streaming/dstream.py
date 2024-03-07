#
# Licensed to the Apache Software Foundation (ASF) under one or more
# contributor license agreements.  See the NOTICE file distributed with
# this work for additional information regarding copyright ownership.
# The ASF licenses this file to You under the Apache License, Version 2.0
# (the "License"); you may not use this file except in compliance with
# the License.  You may obtain a copy of the License at
#
#    http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
#

import operator
import time
from itertools import chain
from datetime import datetime
from typing import (
    Any,
    Callable,
    Generic,
    Hashable,
    Iterable,
    List,
    Optional,
    Tuple,
    TypeVar,
    Union,
    TYPE_CHECKING,
    cast,
    overload,
)

from py4j.protocol import Py4JJavaError
from py4j.java_gateway import JavaObject

from pyspark.storagelevel import StorageLevel
from pyspark.streaming.util import rddToFileName, TransformFunction
from pyspark.rdd import portable_hash, RDD
from pyspark.resultiterable import ResultIterable

if TYPE_CHECKING:
    from pyspark.serializers import Serializer
    from pyspark.streaming.context import StreamingContext

__all__ = ["DStream"]

S = TypeVar("S")
T = TypeVar("T")
T_co = TypeVar("T_co", covariant=True)
U = TypeVar("U")
K = TypeVar("K", bound=Hashable)
V = TypeVar("V")


class DStream(Generic[T_co]):
    """
    A Discretized Stream (DStream), the basic abstraction in Spark Streaming,
    is a continuous sequence of RDDs (of the same type) representing a
    continuous stream of data (see :class:`RDD` in the Spark core documentation
    for more details on RDDs).

    DStreams can either be created from live data (such as, data from TCP
    sockets, etc.) using a :class:`StreamingContext` or it can be
    generated by transforming existing DStreams using operations such as
    `map`, `window` and `reduceByKeyAndWindow`. While a Spark Streaming
    program is running, each DStream periodically generates a RDD, either
    from live data or by transforming the RDD generated by a parent DStream.

    DStreams internally is characterized by a few basic properties:
     - A list of other DStreams that the DStream depends on
     - A time interval at which the DStream generates an RDD
     - A function that is used to generate an RDD after each time interval
    """

    def __init__(
        self,
        jdstream: JavaObject,
        ssc: "StreamingContext",
        jrdd_deserializer: "Serializer",
    ):
        self._jdstream = jdstream
        self._ssc = ssc
        self._sc = ssc._sc
        self._jrdd_deserializer = jrdd_deserializer
        self.is_cached = False
        self.is_checkpointed = False

    def context(self) -> "StreamingContext":
        """
        Return the StreamingContext associated with this DStream
        """
        return self._ssc

    def count(self) -> "DStream[int]":
        """
        Return a new DStream in which each RDD has a single element
        generated by counting each RDD of this DStream.
        """
        return self.mapPartitions(lambda i: [sum(1 for _ in i)]).reduce(operator.add)

    def filter(self: "DStream[T]", f: Callable[[T], bool]) -> "DStream[T]":
        """
        Return a new DStream containing only the elements that satisfy predicate.
        """

        def func(iterator: Iterable[T]) -> Iterable[T]:
            return filter(f, iterator)

        return self.mapPartitions(func, True)

    def flatMap(
        self: "DStream[T]",
        f: Callable[[T], Iterable[U]],
        preservesPartitioning: bool = False,
    ) -> "DStream[U]":
        """
        Return a new DStream by applying a function to all elements of
        this DStream, and then flattening the results
        """

        def func(s: int, iterator: Iterable[T]) -> Iterable[U]:
            return chain.from_iterable(map(f, iterator))

        return self.mapPartitionsWithIndex(func, preservesPartitioning)

    def map(
        self: "DStream[T]", f: Callable[[T], U], preservesPartitioning: bool = False
    ) -> "DStream[U]":
        """
        Return a new DStream by applying a function to each element of DStream.
        """

        def func(iterator: Iterable[T]) -> Iterable[U]:
            return map(f, iterator)

        return self.mapPartitions(func, preservesPartitioning)

    def mapPartitions(
        self: "DStream[T]",
        f: Callable[[Iterable[T]], Iterable[U]],
        preservesPartitioning: bool = False,
    ) -> "DStream[U]":
        """
        Return a new DStream in which each RDD is generated by applying
        mapPartitions() to each RDDs of this DStream.
        """

        def func(s: int, iterator: Iterable[T]) -> Iterable[U]:
            return f(iterator)

        return self.mapPartitionsWithIndex(func, preservesPartitioning)

    def mapPartitionsWithIndex(
        self: "DStream[T]",
        f: Callable[[int, Iterable[T]], Iterable[U]],
        preservesPartitioning: bool = False,
    ) -> "DStream[U]":
        """
        Return a new DStream in which each RDD is generated by applying
        mapPartitionsWithIndex() to each RDDs of this DStream.
        """
        return self.transform(lambda rdd: rdd.mapPartitionsWithIndex(f, preservesPartitioning))

    def reduce(self: "DStream[T]", func: Callable[[T, T], T]) -> "DStream[T]":
        """
        Return a new DStream in which each RDD has a single element
        generated by reducing each RDD of this DStream.
        """
        return self.map(lambda x: (None, x)).reduceByKey(func, 1).map(lambda x: x[1])

    def reduceByKey(
        self: "DStream[Tuple[K, V]]",
        func: Callable[[V, V], V],
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[K, V]]":
        """
        Return a new DStream by applying reduceByKey to each RDD.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism
        return self.combineByKey(lambda x: x, func, func, numPartitions)

    def combineByKey(
        self: "DStream[Tuple[K, V]]",
        createCombiner: Callable[[V], U],
        mergeValue: Callable[[U, V], U],
        mergeCombiners: Callable[[U, U], U],
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[K, U]]":
        """
        Return a new DStream by applying combineByKey to each RDD.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism

        def func(rdd: RDD[Tuple[K, V]]) -> RDD[Tuple[K, U]]:
            return rdd.combineByKey(createCombiner, mergeValue, mergeCombiners, numPartitions)

        return self.transform(func)

    def partitionBy(
        self: "DStream[Tuple[K, V]]",
        numPartitions: int,
        partitionFunc: Callable[[K], int] = portable_hash,
    ) -> "DStream[Tuple[K, V]]":
        """
        Return a copy of the DStream in which each RDD are partitioned
        using the specified partitioner.
        """
        return self.transform(lambda rdd: rdd.partitionBy(numPartitions, partitionFunc))

    @overload
    def foreachRDD(self: "DStream[T]", func: Callable[[RDD[T]], None]) -> None:
        ...

    @overload
    def foreachRDD(self: "DStream[T]", func: Callable[[datetime, RDD[T]], None]) -> None:
        ...

    def foreachRDD(
        self: "DStream[T]",
        func: Union[Callable[[RDD[T]], None], Callable[[datetime, RDD[T]], None]],
    ) -> None:
        """
        Apply a function to each RDD in this DStream.
        """
        if func.__code__.co_argcount == 1:
            old_func = func

            def func(_: datetime, rdd: "RDD[T]") -> None:
                return old_func(rdd)  # type: ignore[call-arg, arg-type]

        jfunc = TransformFunction(self._sc, func, self._jrdd_deserializer)
        assert self._ssc._jvm is not None
        api = self._ssc._jvm.PythonDStream
        api.callForeachRDD(self._jdstream, jfunc)

    def pprint(self, num: int = 10) -> None:
        """
        Print the first num elements of each RDD generated in this DStream.

        Parameters
        ----------
        num : int, optional
            the number of elements from the first will be printed.
        """

        def takeAndPrint(time: datetime, rdd: RDD[T]) -> None:
            taken = rdd.take(num + 1)
            print("-------------------------------------------")
            print("Time: %s" % time)
            print("-------------------------------------------")
            for record in taken[:num]:
                print(record)
            if len(taken) > num:
                print("...")
            print("")

        self.foreachRDD(takeAndPrint)

    def mapValues(self: "DStream[Tuple[K, V]]", f: Callable[[V], U]) -> "DStream[Tuple[K, U]]":
        """
        Return a new DStream by applying a map function to the value of
        each key-value pairs in this DStream without changing the key.
        """

        def map_values_fn(kv: Tuple[K, V]) -> Tuple[K, U]:
            return kv[0], f(kv[1])

        return self.map(map_values_fn, preservesPartitioning=True)

    def flatMapValues(
        self: "DStream[Tuple[K, V]]", f: Callable[[V], Iterable[U]]
    ) -> "DStream[Tuple[K, U]]":
        """
        Return a new DStream by applying a flatmap function to the value
        of each key-value pairs in this DStream without changing the key.
        """

        def flat_map_fn(kv: Tuple[K, V]) -> Iterable[Tuple[K, U]]:
            return ((kv[0], x) for x in f(kv[1]))

        return self.flatMap(flat_map_fn, preservesPartitioning=True)

    def glom(self: "DStream[T]") -> "DStream[List[T]]":
        """
        Return a new DStream in which RDD is generated by applying glom()
        to RDD of this DStream.
        """

        def func(iterator: Iterable[T]) -> Iterable[List[T]]:
            yield list(iterator)

        return self.mapPartitions(func)

    def cache(self: "DStream[T]") -> "DStream[T]":
        """
        Persist the RDDs of this DStream with the default storage level
        (`MEMORY_ONLY`).
        """
        self.is_cached = True
        self.persist(StorageLevel.MEMORY_ONLY)
        return self

    def persist(self: "DStream[T]", storageLevel: StorageLevel) -> "DStream[T]":
        """
        Persist the RDDs of this DStream with the given storage level
        """
        self.is_cached = True
        javaStorageLevel = self._sc._getJavaStorageLevel(storageLevel)
        self._jdstream.persist(javaStorageLevel)
        return self

    def checkpoint(self: "DStream[T]", interval: int) -> "DStream[T]":
        """
        Enable periodic checkpointing of RDDs of this DStream

        Parameters
        ----------
        interval : int
            time in seconds, after each period of that, generated
            RDD will be checkpointed
        """
        self.is_checkpointed = True
        self._jdstream.checkpoint(self._ssc._jduration(interval))
        return self

    def groupByKey(
        self: "DStream[Tuple[K, V]]", numPartitions: Optional[int] = None
    ) -> "DStream[Tuple[K, Iterable[V]]]":
        """
        Return a new DStream by applying groupByKey on each RDD.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism
        return self.transform(lambda rdd: rdd.groupByKey(numPartitions))

    def countByValue(self: "DStream[K]") -> "DStream[Tuple[K, int]]":
        """
        Return a new DStream in which each RDD contains the counts of each
        distinct value in each RDD of this DStream.
        """
        return self.map(lambda x: (x, 1)).reduceByKey(lambda x, y: x + y)

    def saveAsTextFiles(self, prefix: str, suffix: Optional[str] = None) -> None:
        """
        Save each RDD in this DStream as at text file, using string
        representation of elements.
        """

        def saveAsTextFile(t: Optional[datetime], rdd: RDD[T]) -> None:
            path = rddToFileName(prefix, suffix, t)
            try:
                rdd.saveAsTextFile(path)
            except Py4JJavaError as e:
                # after recovered from checkpointing, the foreachRDD may
                # be called twice
                if "FileAlreadyExistsException" not in str(e):
                    raise

        return self.foreachRDD(saveAsTextFile)

    # TODO: uncomment this until we have ssc.pickleFileStream()
    # def saveAsPickleFiles(self, prefix, suffix=None):
    #     """
    #     Save each RDD in this DStream as at binary file, the elements are
    #     serialized by pickle.
    #     """
    #     def saveAsPickleFile(t, rdd):
    #         path = rddToFileName(prefix, suffix, t)
    #         try:
    #             rdd.saveAsPickleFile(path)
    #         except Py4JJavaError as e:
    #             # after recovered from checkpointing, the foreachRDD may
    #             # be called twice
    #             if 'FileAlreadyExistsException' not in str(e):
    #                 raise
    #     return self.foreachRDD(saveAsPickleFile)

    @overload
    def transform(self: "DStream[T]", func: Callable[[RDD[T]], RDD[U]]) -> "TransformedDStream[U]":
        ...

    @overload
    def transform(
        self: "DStream[T]", func: Callable[[datetime, RDD[T]], RDD[U]]
    ) -> "TransformedDStream[U]":
        ...

    def transform(
        self: "DStream[T]",
        func: Union[Callable[[RDD[T]], RDD[U]], Callable[[datetime, RDD[T]], RDD[U]]],
    ) -> "TransformedDStream[U]":
        """
        Return a new DStream in which each RDD is generated by applying a function
        on each RDD of this DStream.

        `func` can have one argument of `rdd`, or have two arguments of
        (`time`, `rdd`)
        """
        if func.__code__.co_argcount == 1:
            oldfunc = func

            def func(_: datetime, rdd: RDD[T]) -> RDD[U]:
                return oldfunc(rdd)  # type: ignore[arg-type, call-arg]

        assert func.__code__.co_argcount == 2, "func should take one or two arguments"
        return TransformedDStream(self, func)

    @overload
    def transformWith(
        self: "DStream[T]",
        func: Callable[[RDD[T], RDD[U]], RDD[V]],
        other: "DStream[U]",
        keepSerializer: bool = ...,
    ) -> "DStream[V]":
        ...

    @overload
    def transformWith(
        self: "DStream[T]",
        func: Callable[[datetime, RDD[T], RDD[U]], RDD[V]],
        other: "DStream[U]",
        keepSerializer: bool = ...,
    ) -> "DStream[V]":
        ...

    def transformWith(
        self: "DStream[T]",
        func: Union[
            Callable[[RDD[T], RDD[U]], RDD[V]],
            Callable[[datetime, RDD[T], RDD[U]], RDD[V]],
        ],
        other: "DStream[U]",
        keepSerializer: bool = False,
    ) -> "DStream[V]":
        """
        Return a new DStream in which each RDD is generated by applying a function
        on each RDD of this DStream and 'other' DStream.

        `func` can have two arguments of (`rdd_a`, `rdd_b`) or have three
        arguments of (`time`, `rdd_a`, `rdd_b`)
        """
        if func.__code__.co_argcount == 2:
            oldfunc = func

            def func(_: datetime, a: RDD[T], b: RDD[U]) -> RDD[V]:
                return oldfunc(a, b)  # type: ignore[call-arg, arg-type]

        assert func.__code__.co_argcount == 3, "func should take two or three arguments"
        jfunc = TransformFunction(
            self._sc,
            func,
            self._jrdd_deserializer,
            other._jrdd_deserializer,
        )
        assert self._sc._jvm is not None
        dstream = self._sc._jvm.PythonTransformed2DStream(
            self._jdstream.dstream(), other._jdstream.dstream(), jfunc
        )
        jrdd_serializer = self._jrdd_deserializer if keepSerializer else self._sc.serializer
        return DStream(dstream.asJavaDStream(), self._ssc, jrdd_serializer)

    def repartition(self: "DStream[T]", numPartitions: int) -> "DStream[T]":
        """
        Return a new DStream with an increased or decreased level of parallelism.
        """
        return self.transform(lambda rdd: rdd.repartition(numPartitions))

    @property
    def _slideDuration(self) -> None:
        """
        Return the slideDuration in seconds of this DStream
        """
        return self._jdstream.dstream().slideDuration().milliseconds() / 1000.0

    def union(self: "DStream[T]", other: "DStream[U]") -> "DStream[Union[T, U]]":
        """
        Return a new DStream by unifying data of another DStream with this DStream.

        Parameters
        ----------
        other : :class:`DStream`
            Another DStream having the same interval (i.e., slideDuration)
            as this DStream.
        """
        if self._slideDuration != other._slideDuration:
            raise ValueError("the two DStream should have same slide duration")
        return self.transformWith(lambda a, b: a.union(b), other, True)

    def cogroup(
        self: "DStream[Tuple[K, V]]",
        other: "DStream[Tuple[K, U]]",
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[K, Tuple[ResultIterable[V], ResultIterable[U]]]]":
        """
        Return a new DStream by applying 'cogroup' between RDDs of this
        DStream and `other` DStream.

        Hash partitioning is used to generate the RDDs with `numPartitions` partitions.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism
        return self.transformWith(
            lambda a, b: a.cogroup(b, numPartitions),
            other,
        )

    def join(
        self: "DStream[Tuple[K, V]]",
        other: "DStream[Tuple[K, U]]",
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[K, Tuple[V, U]]]":
        """
        Return a new DStream by applying 'join' between RDDs of this DStream and
        `other` DStream.

        Hash partitioning is used to generate the RDDs with `numPartitions`
        partitions.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism
        return self.transformWith(lambda a, b: a.join(b, numPartitions), other)

    def leftOuterJoin(
        self: "DStream[Tuple[K, V]]",
        other: "DStream[Tuple[K, U]]",
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[K, Tuple[V, Optional[U]]]]":
        """
        Return a new DStream by applying 'left outer join' between RDDs of this DStream and
        `other` DStream.

        Hash partitioning is used to generate the RDDs with `numPartitions`
        partitions.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism
        return self.transformWith(lambda a, b: a.leftOuterJoin(b, numPartitions), other)

    def rightOuterJoin(
        self: "DStream[Tuple[K, V]]",
        other: "DStream[Tuple[K, U]]",
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[K, Tuple[Optional[V], U]]]":
        """
        Return a new DStream by applying 'right outer join' between RDDs of this DStream and
        `other` DStream.

        Hash partitioning is used to generate the RDDs with `numPartitions`
        partitions.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism
        return self.transformWith(lambda a, b: a.rightOuterJoin(b, numPartitions), other)

    def fullOuterJoin(
        self: "DStream[Tuple[K, V]]",
        other: "DStream[Tuple[K, U]]",
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[K, Tuple[Optional[V], Optional[U]]]]":
        """
        Return a new DStream by applying 'full outer join' between RDDs of this DStream and
        `other` DStream.

        Hash partitioning is used to generate the RDDs with `numPartitions`
        partitions.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism
        return self.transformWith(lambda a, b: a.fullOuterJoin(b, numPartitions), other)

    def _jtime(self, timestamp: Union[datetime, int, float]) -> JavaObject:
        """Convert datetime or unix_timestamp into Time"""
        if isinstance(timestamp, datetime):
            timestamp = time.mktime(timestamp.timetuple())
        assert self._sc._jvm is not None
        return self._sc._jvm.Time(int(timestamp * 1000))

    def slice(self, begin: Union[datetime, int], end: Union[datetime, int]) -> List[RDD[T]]:
        """
        Return all the RDDs between 'begin' to 'end' (both included)

        `begin`, `end` could be datetime.datetime() or unix_timestamp
        """
        jrdds = self._jdstream.slice(self._jtime(begin), self._jtime(end))
        return [RDD(jrdd, self._sc, self._jrdd_deserializer) for jrdd in jrdds]

    def _validate_window_param(self, window: int, slide: Optional[int]) -> None:
        duration = self._jdstream.dstream().slideDuration().milliseconds()
        if int(window * 1000) % duration != 0:
            raise ValueError(
                "windowDuration must be multiple of the parent "
                "dstream's slide (batch) duration (%d ms)" % duration
            )
        if slide and int(slide * 1000) % duration != 0:
            raise ValueError(
                "slideDuration must be multiple of the parent "
                "dstream's slide (batch) duration (%d ms)" % duration
            )

    def window(self, windowDuration: int, slideDuration: Optional[int] = None) -> "DStream[T]":
        """
        Return a new DStream in which each RDD contains all the elements in seen in a
        sliding window of time over this DStream.

        Parameters
        ----------
        windowDuration : int
            width of the window; must be a multiple of this DStream's
            batching interval
        slideDuration : int, optional
            sliding interval of the window (i.e., the interval after which
            the new DStream will generate RDDs); must be a multiple of this
            DStream's batching interval
        """
        self._validate_window_param(windowDuration, slideDuration)
        d = self._ssc._jduration(windowDuration)
        if slideDuration is None:
            return DStream(self._jdstream.window(d), self._ssc, self._jrdd_deserializer)
        s = self._ssc._jduration(slideDuration)
        return DStream(self._jdstream.window(d, s), self._ssc, self._jrdd_deserializer)

    def reduceByWindow(
        self: "DStream[T]",
        reduceFunc: Callable[[T, T], T],
        invReduceFunc: Optional[Callable[[T, T], T]],
        windowDuration: int,
        slideDuration: int,
    ) -> "DStream[T]":
        """
        Return a new DStream in which each RDD has a single element generated by reducing all
        elements in a sliding window over this DStream.

        if `invReduceFunc` is not None, the reduction is done incrementally
        using the old window's reduced value :

        1. reduce the new values that entered the window (e.g., adding new counts)

        2. "inverse reduce" the old values that left the window (e.g., subtracting old counts)
        This is more efficient than `invReduceFunc` is None.

        Parameters
        ----------
        reduceFunc : function
            associative and commutative reduce function
        invReduceFunc : function
            inverse reduce function of `reduceFunc`; such that for all y,
            and invertible x:
            `invReduceFunc(reduceFunc(x, y), x) = y`
        windowDuration : int
            width of the window; must be a multiple of this DStream's
            batching interval
        slideDuration : int
            sliding interval of the window (i.e., the interval after which
            the new DStream will generate RDDs); must be a multiple of this
            DStream's batching interval
        """
        keyed = self.map(lambda x: (1, x))
        reduced = keyed.reduceByKeyAndWindow(
            reduceFunc, invReduceFunc, windowDuration, slideDuration, 1
        )
        return reduced.map(lambda kv: kv[1])

    def countByWindow(
        self: "DStream[T]", windowDuration: int, slideDuration: int
    ) -> "DStream[int]":
        """
        Return a new DStream in which each RDD has a single element generated
        by counting the number of elements in a window over this DStream.
        windowDuration and slideDuration are as defined in the window() operation.

        This is equivalent to window(windowDuration, slideDuration).count(),
        but will be more efficient if window is large.
        """
        return self.map(lambda x: 1).reduceByWindow(
            operator.add, operator.sub, windowDuration, slideDuration
        )

    def countByValueAndWindow(
        self: "DStream[T]",
        windowDuration: int,
        slideDuration: int,
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[T, int]]":
        """
        Return a new DStream in which each RDD contains the count of distinct elements in
        RDDs in a sliding window over this DStream.

        Parameters
        ----------
        windowDuration : int
            width of the window; must be a multiple of this DStream's
            batching interval
        slideDuration : int
            sliding interval of the window (i.e., the interval after which
            the new DStream will generate RDDs); must be a multiple of this
            DStream's batching interval
        numPartitions : int, optional
            number of partitions of each RDD in the new DStream.
        """
        keyed = self.map(lambda x: (x, 1))
        counted = keyed.reduceByKeyAndWindow(
            operator.add, operator.sub, windowDuration, slideDuration, numPartitions
        )
        return counted.filter(lambda kv: kv[1] > 0)

    def groupByKeyAndWindow(
        self: "DStream[Tuple[K, V]]",
        windowDuration: int,
        slideDuration: int,
        numPartitions: Optional[int] = None,
    ) -> "DStream[Tuple[K, Iterable[V]]]":
        """
        Return a new DStream by applying `groupByKey` over a sliding window.
        Similar to `DStream.groupByKey()`, but applies it over a sliding window.

        Parameters
        ----------
        windowDuration : int
            width of the window; must be a multiple of this DStream's
            batching interval
        slideDuration : int
            sliding interval of the window (i.e., the interval after which
            the new DStream will generate RDDs); must be a multiple of this
            DStream's batching interval
        numPartitions : int, optional
            Number of partitions of each RDD in the new DStream.
        """
        ls = self.mapValues(lambda x: [x])
        grouped = ls.reduceByKeyAndWindow(
            lambda a, b: a.extend(b) or a,  # type: ignore[func-returns-value]
            lambda a, b: a[len(b) :],
            windowDuration,
            slideDuration,
            numPartitions,
        )
        return grouped.mapValues(ResultIterable)

    def reduceByKeyAndWindow(
        self: "DStream[Tuple[K, V]]",
        func: Callable[[V, V], V],
        invFunc: Optional[Callable[[V, V], V]],
        windowDuration: int,
        slideDuration: Optional[int] = None,
        numPartitions: Optional[int] = None,
        filterFunc: Optional[Callable[[Tuple[K, V]], bool]] = None,
    ) -> "DStream[Tuple[K, V]]":
        """
        Return a new DStream by applying incremental `reduceByKey` over a sliding window.

        The reduced value of over a new window is calculated using the old window's reduce value :
         1. reduce the new values that entered the window (e.g., adding new counts)
         2. "inverse reduce" the old values that left the window (e.g., subtracting old counts)

        `invFunc` can be None, then it will reduce all the RDDs in window, could be slower
        than having `invFunc`.

        Parameters
        ----------
        func : function
            associative and commutative reduce function
        invFunc : function
            inverse function of `reduceFunc`
        windowDuration : int
            width of the window; must be a multiple of this DStream's
            batching interval
        slideDuration : int, optional
            sliding interval of the window (i.e., the interval after which
            the new DStream will generate RDDs); must be a multiple of this
            DStream's batching interval
        numPartitions : int, optional
            number of partitions of each RDD in the new DStream.
        filterFunc : function, optional
            function to filter expired key-value pairs;
            only pairs that satisfy the function are retained
            set this to null if you do not want to filter
        """
        self._validate_window_param(windowDuration, slideDuration)
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism

        reduced = self.reduceByKey(func, numPartitions)

        if invFunc:

            def reduceFunc(t: datetime, a: Any, b: Any) -> Any:
                b = b.reduceByKey(func, numPartitions)
                r = a.union(b).reduceByKey(func, numPartitions) if a else b
                if filterFunc:
                    r = r.filter(filterFunc)
                return r

            def invReduceFunc(t: datetime, a: Any, b: Any) -> Any:
                b = b.reduceByKey(func, numPartitions)
                joined = a.leftOuterJoin(b, numPartitions)
                return joined.mapValues(
                    lambda kv: invFunc(kv[0], kv[1]) if kv[1] is not None else kv[0]
                )

            jreduceFunc = TransformFunction(self._sc, reduceFunc, reduced._jrdd_deserializer)
            jinvReduceFunc = TransformFunction(self._sc, invReduceFunc, reduced._jrdd_deserializer)
            if slideDuration is None:
                slideDuration = self._slideDuration
            assert self._sc._jvm is not None
            dstream = self._sc._jvm.PythonReducedWindowedDStream(
                reduced._jdstream.dstream(),
                jreduceFunc,
                jinvReduceFunc,
                self._ssc._jduration(windowDuration),
                self._ssc._jduration(slideDuration),  # type: ignore[arg-type]
            )
            return DStream(dstream.asJavaDStream(), self._ssc, self._sc.serializer)
        else:
            return reduced.window(windowDuration, slideDuration).reduceByKey(
                func, numPartitions  # type: ignore[arg-type]
            )

    def updateStateByKey(
        self: "DStream[Tuple[K, V]]",
        updateFunc: Callable[[Iterable[V], Optional[S]], S],
        numPartitions: Optional[int] = None,
        initialRDD: Optional[Union[RDD[Tuple[K, S]], Iterable[Tuple[K, S]]]] = None,
    ) -> "DStream[Tuple[K, S]]":
        """
        Return a new "state" DStream where the state for each key is updated by applying
        the given function on the previous state of the key and the new values of the key.

        Parameters
        ----------
        updateFunc : function
            State update function. If this function returns None, then
            corresponding state key-value pair will be eliminated.
        """
        if numPartitions is None:
            numPartitions = self._sc.defaultParallelism

        if initialRDD and not isinstance(initialRDD, RDD):
            initialRDD = self._sc.parallelize(initialRDD)

        def reduceFunc(t: datetime, a: Any, b: Any) -> Any:
            if a is None:
                g = b.groupByKey(numPartitions).mapValues(lambda vs: (list(vs), None))
            else:
                g = a.cogroup(b.partitionBy(numPartitions), numPartitions)
                g = g.mapValues(lambda ab: (list(ab[1]), list(ab[0])[0] if len(ab[0]) else None))
            state = g.mapValues(lambda vs_s: updateFunc(vs_s[0], vs_s[1]))
            return state.filter(lambda k_v: k_v[1] is not None)

        jreduceFunc = TransformFunction(
            self._sc,
            reduceFunc,
            self._sc.serializer,
            self._jrdd_deserializer,
        )
        if initialRDD:
            initialRDD = cast(RDD[Tuple[K, S]], initialRDD)._reserialize(self._jrdd_deserializer)
            assert self._sc._jvm is not None
            dstream = self._sc._jvm.PythonStateDStream(
                self._jdstream.dstream(),
                jreduceFunc,
                initialRDD._jrdd,
            )
        else:
            assert self._sc._jvm is not None
            dstream = self._sc._jvm.PythonStateDStream(self._jdstream.dstream(), jreduceFunc)

        return DStream(dstream.asJavaDStream(), self._ssc, self._sc.serializer)


class TransformedDStream(DStream[U]):
    """
    TransformedDStream is a DStream generated by an Python function
    transforming each RDD of a DStream to another RDDs.

    Multiple continuous transformations of DStream can be combined into
    one transformation.
    """

    @overload
    def __init__(self: DStream[U], prev: DStream[T], func: Callable[[RDD[T]], RDD[U]]):
        ...

    @overload
    def __init__(
        self: DStream[U],
        prev: DStream[T],
        func: Callable[[datetime, RDD[T]], RDD[U]],
    ):
        ...

    def __init__(
        self,
        prev: DStream[T],
        func: Union[Callable[[RDD[T]], RDD[U]], Callable[[datetime, RDD[T]], RDD[U]]],
    ):
        self._ssc = prev._ssc
        self._sc = self._ssc._sc
        self._jrdd_deserializer = self._sc.serializer
        self.is_cached = False
        self.is_checkpointed = False
        self._jdstream_val = None

        # Using type() to avoid folding the functions and compacting the DStreams which is not
        # not strictly an object of TransformedDStream.
        if type(prev) is TransformedDStream and not prev.is_cached and not prev.is_checkpointed:
            prev_func: Callable = prev.func
            func = cast(Callable[[datetime, RDD[T]], RDD[U]], func)
            self.func: Union[
                Callable[[RDD[T]], RDD[U]], Callable[[datetime, RDD[T]], RDD[U]]
            ] = lambda t, rdd: func(t, prev_func(t, rdd))
            self.prev: DStream[T] = prev.prev
        else:
            self.prev = prev
            self.func = func

    @property
    def _jdstream(self) -> JavaObject:
        if self._jdstream_val is not None:
            return self._jdstream_val

        jfunc = TransformFunction(self._sc, self.func, self.prev._jrdd_deserializer)
        assert self._sc._jvm is not None
        dstream = self._sc._jvm.PythonTransformedDStream(self.prev._jdstream.dstream(), jfunc)
        self._jdstream_val = dstream.asJavaDStream()
        return self._jdstream_val
