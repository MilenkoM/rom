
from datetime import datetime, date, time as dtime
from decimal import Decimal as _Decimal
import json

from .exceptions import (ORMError, InvalidOperation, ColumnError,
    MissingColumn, InvalidColumnValue)
from .util import (_numeric_keygen, _string_keygen, _many_to_one_keygen,
    _boolean_keygen, dt2ts, ts2dt, t2ts, ts2t, session, _connect)

NULL = object()
MODELS = {}
_NUMERIC = (0, 0.0, _Decimal('0'), datetime(1970, 1, 1), date(1970, 1, 1), dtime(0, 0, 0))

class Column(object):
    '''
    Column objects handle data conversion to/from strings, store metadata
    about indices, etc. Note that these are "heavy" columns, in that whenever
    data is read/written, it must go through descriptor processing. This is
    primarily so that (for example) if you try to write a Decimal to a Float
    column, you get an error the moment you try to do it, not some time later
    when you try to save the object (though saving can still cause an error
    during the conversion process).

    Standard Arguments:

        * *required* - determines whether this column is required on
          creation
        * *default* - a default value (either a callable or a simple value)
          when this column is not provided
        * *unique* - can only be enabled on ``String`` columns, allows for
          required distinct column values (like an email address on a User
          model)
        * *index* - can be enabled on numeric, string, and unicode columns.
          Will create a ZSET-based numeric index for numeric columns and a
          "full word"-based search for string/unicode columns. If enabled
          for other (or custom) columns, remember to provide the
          ``keygen`` argument
        * *keygen* - pass a function that takes your column's value and
          returns the data that you want to index (see the keygen docs for
          what kinds of data to return)

    Notes:

        * Columns with 'unique' set to True can only be string columns
        * You can only have one unique column on any model
        * Unique and index are not mutually exclusive
        * The keygen argument determines how index values are generated
          from column data (with reasonably sensible defaults for numeric
          and string columns)
        * If you set required to True, then you must have the column set
          during object construction: ``MyModel(col=val)``
    '''
    _allowed = ()
    _default_ = None

    __slots__ = '_required _default _init _unique _index _model _attr _keygen'.split()

    def __init__(self, required=False, default=NULL, unique=False, index=False, keygen=None):
        self._required = required
        self._default = default
        self._unique = unique
        self._index = index
        self._init = False
        self._model = None
        self._attr = None
        self._keygen = None

        if unique:
            if self._allowed != str and self._allowed != unicode:
                raise ColumnError("Unique columns can only be strings")

        numeric = True
        if index and not isinstance(self, ManyToOne):
            if not any(isinstance(i, self._allowed) for i in _NUMERIC):
                numeric = False
                if isinstance(True, self._allowed):
                    keygen = keygen or _boolean_keygen
                if self._allowed not in (str, unicode) and not keygen:
                    raise ColumnError("Non-numeric/string indexed columns must provide keygen argument on creation")

        if index:
            self._keygen = keygen if keygen else (
                _numeric_keygen if numeric else _string_keygen)

    def _from_redis(self, value):
        convert = self._allowed[0] if isinstance(self._allowed, (tuple, list)) else self._allowed
        return convert(value)

    def _to_redis(self, value):
        if isinstance(value, long):
            return str(value)
        return repr(value)

    def _validate(self, value):
        if value is not None:
            if isinstance(value, self._allowed):
                return
        elif not self._required:
            return
        raise InvalidColumnValue("%s.%s has type %r but must be of type %r"%(
            self._model, self._attr, type(value), self._allowed))

    def _init_(self, obj, model, attr, value, loading):
        # You shouldn't be calling this directly, but this is what sets up all
        # of the necessary pieces when creating an entity from scratch, or
        # loading the entity from Redis
        self._model = model
        self._attr = attr

        if value is None:
            if self._default is NULL:
                if self._required:
                    raise MissingColumn("%s.%s cannot be missing"%(self._model, self._attr))
            elif callable(self._default):
                value = self._default()
            else:
                value = self._default
        elif not isinstance(value, self._allowed):
            try:
                value = self._from_redis(value)
            except (ValueError, TypeError) as e:
                raise InvalidColumnValue(*e.args)

        if not loading:
            self._validate(value)
        obj._data[attr] = value

    def __set__(self, obj, value):
        if not obj._init:
            self._init_(obj, *value)
            return
        try:
            value = self._from_redis(value)
        except (ValueError, TypeError):
            raise InvalidColumnValue("Cannot convert %r into type %s"%(value, self._allowed))
        self._validate(value)
        obj._data[self._attr] = value
        obj._modified = True
        session.add(obj)

    def __get__(self, obj, objtype):
        try:
            return obj._data[self._attr]
        except KeyError:
            AttributeError("%s.%s does not exist"%(self._model, self._attr))

    def __delete__(self, obj):
        if self._required:
            raise InvalidOperation("%s.%s cannot be null"%(self._model, self._attr))
        try:
            obj._data.pop(self._attr)
        except KeyError:
            raise AttributeError("%s.%s does not exist"%(self._model, self._attr))
        obj._modified = True
        session.add(obj)

class Integer(Column):
    '''
    Used for integer numeric columns.

    All standard arguments supported.

    Used via::

        class MyModel(Model):
            col = Integer()
    '''
    _allowed = (int, long)

class Boolean(Column):
    '''
    Used for boolean columns.

    All standard arguments supported.

    All values passed in on creation are casted via bool(), with the exception
    of None (which behaves as though the value was missing), and any existing
    data in Redis is considered ``False`` if empty, and ``True`` otherwise.

    Used via::

        class MyModel(Model):
            col = Boolean()

    Queries via ``MyModel.get_by(...)`` and ``MyModel.query.filter(...)`` work
    as expected when passed ``True`` or ``False``.

    .. note: these columns are not sortable by default.
    '''
    _allowed = bool
    def _to_redis(self, obj):
        return '1' if obj else ''
    def _from_redis(self, obj):
        return bool(obj)

class Float(Column):
    '''
    Numeric column that supports integers and floats (values are turned into
    floats on load from Redis).

    All standard arguments supported.

    Used via::

        class MyModel(Model):
            col = Float()
    '''
    _allowed = (float, int, long)

class Decimal(Column):
    '''
    A Decimal-only numeric column (converts ints/longs into Decimals
    automatically). Attempts to assign Python float will fail.

    All standard arguments supported.

    Used via::

        class MyModel(Model):
            col = Decimal()
    '''
    _allowed = _Decimal
    def _from_redis(self, value):
        return _Decimal(value)
    def _to_redis(self, value):
        return str(value)

class DateTime(Column):
    '''
    A datetime column.

    All standard arguments supported.

    Used via::

        class MyModel(Model):
            col = DateTime()

    .. note:: tzinfo objects are not stored
    '''
    _allowed = datetime
    def _from_redis(self, value):
        return ts2dt(float(value))
    def _to_redis(self, value):
        return repr(dt2ts(value))

class Date(Column):
    '''
    A date column.

    All standard arguments supported.

    Used via::

        class MyModel(Model):
            col = Date()
    '''
    _allowed = date
    def _from_redis(self, value):
        return ts2dt(float(value)).date()
    def _to_redis(self, value):
        return repr(dt2ts(value))

class Time(Column):
    '''
    A time column. Timezones are ignored.

    All standard arguments supported.

    Used via::

        class MyModel(Model):
            col = Time()

    .. note:: tzinfo objects are not stored
    '''
    _allowed = dtime
    def _from_redis(self, value):
        return ts2t(float(value))
    def _to_redis(self, value):
        return repr(t2ts(value))

class String(Column):
    '''
    A plain string column. Trying to save unicode strings will probably result
    in an error, if not bad data. This is the only type of column that can
    have a unique index.

    All standard arguments supported.

    This column can be indexed, which will allow for searching for words
    contained in the column, extracted via::

        filter(None, [s.lower().strip(string.punctuation) for s in val.split()])

    .. note:: only one column in any given model can be unique.

    Used via::

        class MyModel(Model):
            col = String()
    '''
    _allowed = str
    def _to_redis(self, value):
        return value

class Text(Column):
    '''
    A unicode string column.

    All standard arguments supported, except for ``unique``.

    Aside from not supporting ``unique`` indices, will generally have the same
    behavior as a ``String`` column, only supporting unicode strings. Data is
    encoded via utf-8 before writing to Redis. If you would like to create
    your own column to encode/decode differently, examine the source find out
    how to do it.

    Used via::

        class MyModel(Model):
            col = Text()
    '''
    _allowed = unicode
    def _to_redis(self, value):
        return value.encode('utf-8')
    def _from_redis(self, value):
        if isinstance(value, str):
            return value.decode('utf-8')
        return value

class Json(Column):
    '''
    Allows for more complicated nested structures as attributes.

    All standard arguments supported. The ``keygen`` argument must be provided
    if ``index`` is ``True``.

    Used via::

        class MyModel(Model):
            col = Json()
    '''
    _allowed = (dict, list, tuple)
    def _to_redis(self, value):
        return json.dumps(value)
    def _from_redis(self, value):
        if isinstance(value, self._allowed):
            return value
        return json.loads(value)

class PrimaryKey(Column):
    '''
    This is a primary key column, used when you want the primary key to be
    named something other than 'id'. If you omit a PrimaryKey column on your
    Model classes, one will be automatically cretaed for you.

    Only the ``index`` argument will be used. You may want to enable indexing
    on this column if you want to be able to perform queries and sort the
    results by primary key.

    Used via:

        class MyModel(Model):
            id = PrimaryKey()
    '''
    _allowed = (int, long)

    def __init__(self, index=False):
        Column.__init__(self, required=False, default=None, unique=False, index=index)

    def _init_(self, obj, model, attr, value, loading):
        self._model = model
        self._attr = attr
        if value is None:
            value = _connect(obj).incr('%s:%s:'%(model, attr))
            obj._modified = True
        else:
            value = int(value)
        obj._data[attr] = value
        session.add(obj)

    def __set__(self, obj, value):
        if not obj._init:
            self._init_(obj, *value)
            return
        raise InvalidOperation("Cannot update primary key value")

class ManyToOne(Column):
    '''
    This ManyToOne column allows for one model to reference another model.
    While a ManyToOne column does not require a reverse OneToMany column
    (which will return a list of models that reference it via a ManyToOne), it
    is generally seen as being useful to have both sides of the relationship
    defined.

    Aside from the name of the other model, only the ``required`` and
    ``default`` arguments are accepted.

    Used via::

        class MyModel(Model):
            col = ManyToOne('OtherModelName')

    .. note: Technically, all ``ManyToOne`` columns are indexed numerically,
      which means that you can find entities with specific id ranges or even
      sort by the ids referenced.

    '''
    __slots__ = Column.__slots__ + ['_ftable']
    def __init__(self, ftable, required=False, default=NULL):
        self._ftable = ftable
        Column.__init__(self, required, default, index=True, keygen=_many_to_one_keygen)

    def _from_redis(self, value):
        try:
            model = MODELS[self._ftable]
        except KeyError:
            raise ORMError("Missing foreign table %r referenced by %s.%s"%(self._ftable, self._model, self._attr))
        if isinstance(value, model):
            return value
        return model.get(value)

    def _validate(self, value):
        try:
            model = MODELS[self._ftable]
        except KeyError:
            raise ORMError("Missing foreign table %r referenced by %s.%s"%(self._ftable, self._model, self._attr))
        if not self._required and value is None:
            return
        if not isinstance(value, model):
            raise InvalidColumnValue("%s.%s has type %r but must be of type %r"%(
                self._model, self._attr, type(value), model))

    def _to_redis(self, value):
        if not value:
            return None
        if isinstance(value, (int, long)):
            return str(value)
        if value._new:
            # should spew a warning here
            value.save()
        v = str(getattr(value, value._pkey))
        return v

class ForeignModel(Column):
    '''
    This column allows for ``rom`` models to reference an instance of another
    model from an unrelated ORM or otherwise.

    .. note: In order for this mechanism to work, the foreign model *must*
      have an ``id`` attribute or property represents its primary key, and
      *must* have a classmethod or staticmethod named ``get()`` that returns
      the proper database entity.

    Used via::

        class MyModel(Model):
            col = ForeignModel(DjangoModel)

        dm = DjangoModel(col1='foo')
        django.db.transaction.commit()

        x = MyModel(col=dm)
        x.save()
    '''
    __slots__ = Column.__slots__ + ['_fmodel']
    def __init__(self, fmodel, required=False, default=NULL):
        self._fmodel = fmodel
        Column.__init__(self, required, default, index=True, keygen=_many_to_one_keygen)

    def _from_redis(self, value):
        if isinstance(value, self._fmodel):
            return value
        if isinstance(value, str) and value.isdigit():
            value = int(value, 10)
        return self._fmodel.get(value)

    def _validate(self, value):
        if not self._required and value is None:
            return
        if not isinstance(value, self._fmodel):
            raise InvalidColumnValue("%s.%s has type %r but must be of type %r"%(
                self._model, self._attr, type(value), self._fmodel))

    def _to_redis(self, value):
        if not value:
            return None
        if isinstance(value, (int, long, str)):
            return str(value)
        return str(value.id)

class OneToMany(Column):
    '''
    OneToMany columns do not actually store any information. They rely on a
    properly defined reverse ManyToOne column on the referenced model in order
    to be able to fetch a list of referring entities.

    Only the name of the other model can be passed.

    Used via::

        class MyModel(Model):
            col = OneToMany('OtherModelName')
    '''
    __slots__ = '_model _attr _ftable _required _unique _index'.split()
    def __init__(self, ftable):
        self._ftable = ftable
        self._required = self._unique = self._index = False
        self._model = self._attr = None

    def _to_redis(self, value):
        return ''

    def __set__(self, obj, value):
        if not obj._init:
            self._model, self._attr = value[:2]
            try:
                MODELS[self._ftable]
            except KeyError:
                raise ORMError("Missing foreign table %r referenced by %s.%s"%(self._ftable, self._model, self._attr))
            return
        raise InvalidOperation("Cannot assign to OneToMany relationships")

    def __get__(self, obj, objtype):
        try:
            model = MODELS[self._ftable]
        except KeyError:
            raise ORMError("Missing foreign table %r referenced by %s.%s"%(self._ftable, self._model, self._attr))

        for attr, col in model._columns.iteritems():
            if isinstance(col, ManyToOne) and col._ftable == self._model:
                return model.get_by(**{attr: getattr(obj, obj._pkey)})

        raise ORMError("Reverse ManyToOne relationship not found for %s.%s -> %s"%(self._model, self._attr, self._ftable))

    def __delete__(self, obj):
        raise InvalidOperation("Cannot delete OneToMany relationships")
