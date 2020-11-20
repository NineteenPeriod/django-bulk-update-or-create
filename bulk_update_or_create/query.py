from functools import partial, reduce

from django.db import models


class BulkUpdateOrCreateMixin:
    def bulk_update_or_create_context(
        self,
        update_fields,
        match_field='pk',
        batch_size=100,
        case_insensitive_match=False,
        status_cb=None,
    ):
        """
        Helper method that returns a context manager (_BulkUpdateOrCreateContextManager) that makes it easier to handle
        a stream of objects with unknown size.
        Call `.queue(obj)` and whenever `batch_size` is reached or the context terminates, this context manager will call
        `bulk_update_or_create` on the queue

        :param update_fields: fields that will be updated if record already exists (passed on to bulk_update)
        :param match_field: model field that will match existing records (defaults to "pk")
        :param batch_size: number of records to process in each batch (defaults to 100)
        :param case_insensitive_match: set to True if using MySQL with "ci" collations (defaults to False)
        :param status_cb: if set to a callable, status_cb is called a tuple of lists with ([created], [updated]) objects as they're yielded
        """
        return _BulkUpdateOrCreateContextManager(
            self,
            update_fields,
            batch_size=batch_size,
            status_cb=status_cb,
            match_field=match_field,
            case_insensitive_match=case_insensitive_match,
        )

    def bulk_update_or_create(
        self,
        objs,
        update_fields,
        match_field='pk',
        batch_size=None,
        case_insensitive_match=False,
        yield_objects=False,
    ):
        """

        :param objs: model instances to be updated or created
        :param update_fields: fields that will be updated if record already exists (passed on to bulk_update)
        :param match_field: model fields that will match existing records (defaults to ["pk"])
        :param batch_size: number of records to process in each batch (defaults to len(objs))
        :param case_insensitive_match: set to True if using MySQL with "ci" collations (defaults to False)
        :param yield_objects: if True, method becomes a generator that will yield a tuple of lists with ([created], [updated]) objects
        """

        r = self.__bulk_update_or_create(
            objs,
            update_fields,
            match_field,
            batch_size,
            case_insensitive_match,
            yield_objects,
        )
        if yield_objects:
            return r
        return list(r)

    def __bulk_update_or_create(
        self,
        objs,
        update_fields,
        match_field='pk',
        batch_size=None,
        case_insensitive_match=False,
        yield_objects=False,
    ):
        if not objs:
            raise ValueError('no objects to update_or_create...')
        if not update_fields:
            raise ValueError('update_fields cannot be empty')

        # generators not supported (for now?), as bulk_update doesn't either
        objs = list(objs)
        if batch_size is None:
            batch_size = len(objs)

        match_field = (match_field,) if isinstance(match_field, str) else match_field

        # validate that all objects have the required fields
        for obj in objs:
            if not all(map(partial(hasattr, obj), match_field)):
                raise ValueError(
                    f'some object does not have the match_field {", ".join(match_field)}'
                )
            for _f in update_fields:
                if not hasattr(obj, _f):
                    raise ValueError(f'some object does not have the update_field {_f}')

        batches = (objs[i : i + batch_size] for i in range(0, len(objs), batch_size))

        def _obj_key_getter(obj):  # no-op
            return tuple(map(
                partial(getattr, obj),
                match_field
            ))

        _obj_key_getter_sensitive = _obj_key_getter

        if case_insensitive_match:
            def _obj_key_getter(obj):
                return tuple(map(
                    lambda v: v.lower() if hasattr(v, 'lower') else v,
                    _obj_key_getter_sensitive(obj),
                ))

        def _obj_filter(obj_map):
            return reduce(
                lambda acc_q, obj_key: acc_q | models.Q(**{
                    k: obj_key[i] for i, k in enumerate(match_field)
                }),
                obj_map.keys(),
                models.Q(),
            )

        for batch in batches:
            obj_map = {_obj_key_getter(obj): obj for obj in batch}

            # mass select for bulk_update on existing ones
            to_update = list(self.filter(_obj_filter(obj_map=obj_map)))
            for to_u in to_update:
                obj = obj_map[_obj_key_getter(to_u)]
                for _f in update_fields:
                    setattr(to_u, _f, getattr(obj, _f))
                del obj_map[_obj_key_getter(to_u)]
            self.bulk_update(to_update, update_fields)

            # .create on the remaining (bulk_create won't work on multi-table inheritance models...)
            created_objs = []
            for obj in obj_map.values():
                obj.save()
                created_objs.append(obj)
            if yield_objects:
                yield created_objs, to_update


class BulkUpdateOrCreateQuerySet(BulkUpdateOrCreateMixin, models.QuerySet):
    pass


class _BulkUpdateOrCreateContextManager:
    def __init__(
        self, queryset, update_fields, batch_size=500, status_cb=None, **kwargs
    ):
        self._queue = []
        self._queryset = queryset
        self._batch_size = batch_size
        assert status_cb is None or callable(status_cb)
        self._cb = status_cb
        self._fields = update_fields
        self._kwargs = kwargs

    def queue(self, obj):
        self._queue.append(obj)
        if len(self._queue) >= self._batch_size:
            self.dump_queue()

    def dump_queue(self):
        if not self._queue:
            return

        r = self._queryset.bulk_update_or_create(
            self._queue,
            self._fields,
            yield_objects=self._cb is not None,
            **self._kwargs,
        )
        if self._cb is not None:
            for st in r:
                self._cb(st)

        self._queue = []

    def __enter__(self):
        return self

    def __exit__(self, type, value, traceback):
        self.dump_queue()
