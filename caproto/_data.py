# These classes ChannelData classes hold the state associated with the data
# source underlying a Channel, including data values, alarm state, and
# metadata. They perform data type conversions in response to requests to read
# data as a certain type, and they push updates into queues registered by a
# higher-level server.
import copy
import time
import weakref
from collections import defaultdict, namedtuple
from collections.abc import Iterable

from ._backend import backend
from ._commands import parse_metadata
from ._constants import MAX_ENUM_STATES, MAX_ENUM_STRING_SIZE
from ._dbr import (DBR_STSACK_STRING, DBR_TYPES, AccessRights, AlarmSeverity,
                   AlarmStatus, ChannelType, GraphicControlBase,
                   SubscriptionType, TimeStamp, _channel_type_by_name,
                   _LongStringChannelType, native_type, native_types,
                   time_types)
from ._utils import (CaprotoError, CaprotoValueError, ConversionDirection,
                     is_array_read_only)

__all__ = ('Forbidden',
           'ChannelAlarm',
           'ChannelByte',
           'ChannelChar',
           'ChannelData',
           'ChannelDouble',
           'ChannelEnum',
           'ChannelFloat',
           'ChannelInteger',
           'ChannelNumeric',
           'ChannelShort',
           'ChannelString',
           'SkipWrite',
           )

SubscriptionUpdate = namedtuple('SubscriptionUpdate',
                                ('sub_specs', 'metadata', 'values',
                                 'flags', 'sub'))


class Forbidden(CaprotoError):
    ...


class CannotExceedLimits(CaprotoValueError):
    ...


class SkipWrite(Exception):
    """Raise this exception to skip further processing of write()."""


def dbr_metadata_to_dict(dbr_metadata, string_encoding):
    '''Return a dictionary of metadata keys to values'''
    info = dbr_metadata.to_dict()

    try:
        info['units'] = info['units'].decode(string_encoding)
    except KeyError:
        ...

    return info


def _read_only_property(key, doc=None):
    '''Create property that gives read-only access to instance._data[key]'''
    if doc is None:
        doc = f"Read-only access to {key} data"
    return property(lambda self: self._data[key], doc=doc)


class ChannelAlarm:
    def __init__(self, *, status=0, severity=0,
                 must_acknowledge_transient=True, severity_to_acknowledge=0,
                 alarm_string='', string_encoding='latin-1'):
        """
        Alarm metadata that can be used by one or more ChannelData instances.

        Parameters
        ----------
        status : int or AlarmStatus, optional
            Status information.
        severity : int or AlarmSeverity, optional
            Severity information.
        must_acknowledge_transient : int, optional
            Whether or not transient alarms must be acknowledged.
        severity_to_acknowledge : int, optional
            The highest alarm severity to acknowledge. If the current alarm
            severity is less then or equal to this value the alarm is
            acknowledged.
        alarm_string : str, optional
            Alarm information.
        string_encoding : str, optional
            String encoding of the alarm string.
        """
        self._channels = weakref.WeakSet()
        self.string_encoding = string_encoding
        self._data = dict(
            status=status, severity=severity,
            must_acknowledge_transient=must_acknowledge_transient,
            severity_to_acknowledge=severity_to_acknowledge,
            alarm_string=alarm_string)

    def __getnewargs_ex__(self):
        kwargs = {
            'status': self.status,
            'severity': self.severity,
            'must_acknowledge_transient': self.must_acknowledge_transient,
            'severity_to_acknowledge': self.severity_to_acknowledge,
            'alarm_string': self.alarm_string,
            'string_encoding': self.string_encoding,
        }
        return ((), kwargs)

    status = _read_only_property('status',
                                 doc='Current alarm status')
    severity = _read_only_property('severity',
                                   doc='Current alarm severity')
    must_acknowledge_transient = _read_only_property(
        'must_acknowledge_transient',
        doc='Toggle whether or not transient alarms must be acknowledged')

    severity_to_acknowledge = _read_only_property(
        'severity_to_acknowledge',
        doc='The alarm severity that has been acknowledged')

    alarm_string = _read_only_property('alarm_string',
                                       doc='String associated with alarm')

    def __repr__(self):
        return f'<ChannelAlarm(status={self.status}, severity={self.severity})>'

    def connect(self, channel_data):
        """Add a ChannelData instance to the channel set using this alarm."""
        self._channels.add(channel_data)

    def disconnect(self, channel_data):
        """Remove ChannelData instance from channel set using this alarm."""
        self._channels.remove(channel_data)

    async def read(self, dbr=None):
        """Read alarm information into a DBR_STSACK_STRING instance."""
        if dbr is None:
            dbr = DBR_STSACK_STRING()
        dbr.status = self.status
        dbr.severity = self.severity
        dbr.ackt = 1 if self.must_acknowledge_transient else 0
        dbr.acks = self.severity_to_acknowledge
        dbr.value = self.alarm_string.encode(self.string_encoding)
        return dbr

    async def write(self, *, status=None, severity=None,
                    must_acknowledge_transient=None,
                    severity_to_acknowledge=None,
                    alarm_string=None, flags=0, publish=True):
        """
        Write data to the alarm and optionally publish it to clients.

        Parameters
        ----------
        status : int or AlarmStatus, optional
            Status information.
        severity : int or AlarmSeverity, optional
            Severity information.
        must_acknowledge_transient : int, optional
            Whether or not transient alarms must be acknowledged.
        severity_to_acknowledge : int, optional
            The highest alarm severity to acknowledge. If the current alarm
            severity is less then or equal to this value the alarm is
            acknowledged.
        alarm_string : str, optional
            Alarm information.
        flags : SubscriptionType or int, optional
            Subscription flags.
        publish : bool, optional
            Optionally publish the alarm status after the write.
        """
        data = self._data

        if status is not None:
            data['status'] = AlarmStatus(status)
            flags |= SubscriptionType.DBE_VALUE

        if severity is not None:
            data['severity'] = AlarmSeverity(severity)

            if (not self.must_acknowledge_transient or
                    self.severity_to_acknowledge < self.severity):
                data['severity_to_acknowledge'] = self.severity

            flags |= SubscriptionType.DBE_ALARM

        if must_acknowledge_transient is not None:
            data['must_acknowledge_transient'] = must_acknowledge_transient
            if (not must_acknowledge_transient and
                    self.severity_to_acknowledge > self.severity):
                # Reset the severity to acknowledge if disabling transient
                # requirement
                data['severity_to_acknowledge'] = self.severity
            flags |= SubscriptionType.DBE_ALARM

        if severity_to_acknowledge is not None:
            # To clear, set greater than or equal to the
            # severity_to_acknowledge
            if severity_to_acknowledge >= self.severity:
                data['severity_to_acknowledge'] = 0
                flags |= SubscriptionType.DBE_ALARM

        if alarm_string is not None:
            data['alarm_string'] = alarm_string
            flags |= SubscriptionType.DBE_ALARM

        if publish:
            await self.publish(flags)

    async def publish(self, flags, *, except_for=None):
        """
        Publish alarm information to all listening channels.

        Parameters
        ----------
        flags : SubscriptionType
            The subscription type to publish.
        except_for : sequence of ChannelData, optional
            Skip publishing to these channels, mainly to avoid recursion.
        """
        except_for = except_for or ()
        for channel in self._channels:
            if channel not in except_for:
                await channel.publish(flags)


class ChannelData:
    """
    Base class holding data and metadata which can be sent across a Channel.

    Parameters
    ----------
    value :
        Data which has to match with this class's ``data_type``.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp associated with the value. Defaults to ``time.time()``.
        Raw EPICS timestamps are also supported in the form of
        ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual
        record or be set to something arbitrary.  Defaults to 'caproto'.
    """
    data_type = ChannelType.LONG
    _compatible_array_types = {}

    def __init__(self, *, alarm=None, value=None, timestamp=None,
                 max_length=None, string_encoding='latin-1',
                 reported_record_type='caproto'):
        if timestamp is None:
            timestamp = time.time()
        if alarm is None:
            alarm = ChannelAlarm()

        self._alarm = None
        self._status = None
        self._severity = None

        # now use the setter to attach the alarm correctly:
        self.alarm = alarm
        self._max_length = max_length
        self.string_encoding = string_encoding
        self.reported_record_type = reported_record_type

        if self._max_length is None:
            # Use the current length as maximum, if unspecified.
            self._max_length = max(self.calculate_length(value), 1)
            # It is possible to pass in a zero-length array to start with.
            # However, it is not useful to have an empty value forever, so the
            # minimum length here is required to be at least 1.

        value = self.preprocess_value(value)

        # The following _data isn't meant to be modified directly. It should be
        # modified by way of the ``write()`` and ``write_metadata`` methods.
        self._data = dict(
            value=value,
            timestamp=TimeStamp.from_flexible_value(timestamp),
        )
        # This is a dict keyed on queues that will receive subscription
        # updates.  (Each queue belongs to a Context.) Each value is itself a
        # dict, mapping data_types to the set of SubscriptionSpecs that request
        # that data_type.
        self._queues = defaultdict(
            lambda: defaultdict(
                lambda: defaultdict(set)))

        # Cache results of data_type conversions. This maps data_type to
        # (metdata, value). This is cleared each time publish() is called.
        self._content = {}
        self._snapshots = defaultdict(dict)
        self._fill_at_next_write = list()

    def calculate_length(self, value):
        'Calculate the number of elements given a value'
        is_array = isinstance(value, (list, tuple) + backend.array_types)
        if is_array:
            return len(value)
        if isinstance(value, (bytes, str, bytearray)):
            if self.data_type == ChannelType.CHAR:
                return len(value)
        return 1

    @property
    def length(self):
        'The number of elements (length) of the current value'
        return self.calculate_length(self.value)

    @property
    def max_length(self):
        'The maximum number of elements (length) this channel can hold'
        return self._max_length

    def preprocess_value(self, value):
        '''Pre-process values destined for verify_value and write

        A few basic things are done here, based on max_count:
        1. If length >= 2, ensure the value is a list
        2. If length == 1, ensure the value is an unpacked scalar
        3. Ensure len(value) <= length

        Raises
        ------
        CaprotoValueError
        '''
        is_array = isinstance(value, (list, tuple) + backend.array_types)
        if is_array:
            if len(value) > self._max_length:
                # TODO consider an exception for caproto-only environments that
                # can handle dynamically resized arrays (i.e., sizes greater
                # than the initial max_length)?
                raise CaprotoValueError(
                    f'Value of length {len(value)} is too large for '
                    f'{self.__class__.__name__}(max_length={self._max_length})'
                )

        if self._max_length == 1:
            if is_array:
                if len(value):
                    # scalar value in a list -> scalar value
                    return value[0]
                raise CaprotoValueError(
                    'Cannot set a scalar to an empty array')
        elif not is_array:
            # scalar value that should be in a list -> list
            return [value]
        return value

    def __getnewargs_ex__(self):
        # ref: https://docs.python.org/3/library/pickle.html
        kwargs = {
            'timestamp': self.epics_timestamp,
            'alarm': self.alarm,
            'string_encoding': self.string_encoding,
            'reported_record_type': self.reported_record_type,
            'data': self._data,
            'max_length': self._max_length
        }
        return ((), kwargs)

    value = _read_only_property('value')

    # "before" — only the last value received before the state changes from
    #     false to true is forwarded to the client.
    # "first" — only the first value received after the state changes from true
    #     to false is forwarded to the client.
    # "while" — values are forwarded to the client as long as the state is true.
    # "last" — only the last value received before the state changes from true
    #     to false is forwarded to the client.
    # "after" — only the first value received after the state changes from true
    #     to false is forwarded to the client.
    # "unless" — values are forwarded to the client as long as the state is
    #     false.

    def pre_state_change(self, state, new_value):
        "This is called by the server when it enters its StateUpdateContext."
        snapshots = self._snapshots[state]
        snapshots.clear()
        snapshot = copy.deepcopy(self)
        if new_value:
            # We are changing from false to true.
            snapshots['before'] = snapshot
        else:
            # We are changing from true to false.
            snapshots['last'] = snapshot

    def post_state_change(self, state, new_value):
        "This is called by the server when it exits its StateUpdateContext."
        snapshots = self._snapshots[state]
        if new_value:
            # We have changed from false to true.
            snapshots['while'] = self
            self._fill_at_next_write.append((state, 'after'))
        else:
            # We have changed from true to false.
            snapshots['unless'] = self
            self._fill_at_next_write.append((state, 'first'))

    @property
    def alarm(self):
        'The ChannelAlarm associated with this data'
        return self._alarm

    @alarm.setter
    def alarm(self, alarm):
        old_alarm = self._alarm
        if old_alarm is alarm:
            return

        if old_alarm is not None:
            old_alarm.disconnect(self)

        self._alarm = alarm
        if alarm is not None:
            alarm.connect(self)

    async def subscribe(self, queue, sub_spec, sub):
        """
        Subscribe a queue for the given subscription specification.

        Parameters
        ----------
        queue : asyncio.Queue or compatible
            The queue to send data to.
        sub_spec : SubscriptionSpec
            The matching subscription specification.
        sub : Subscription
            The subscription instance.
        """
        by_sync = self._queues[queue][sub_spec.channel_filter.sync]
        by_sync[sub_spec.data_type_name].add(sub_spec)

        # Always send current reading immediately upon subscription.
        try:
            metadata, values = self._content[sub_spec.data_type_name]
        except KeyError:
            # Do the expensive data type conversion and cache it in case
            # a future subscription wants the same data type.
            data_type = _channel_type_by_name[sub_spec.data_type_name]
            metadata, values = await self._read(data_type)
            self._content[sub_spec.data_type_name] = metadata, values
        await queue.put(SubscriptionUpdate((sub_spec,), metadata, values, 0, sub))

    async def unsubscribe(self, queue, sub_spec):
        """
        Unsubscribe a queue for the given subscription specification.

        Parameters
        ----------
        queue : asyncio.Queue or compatible
            The queue to send data to.
        sub_spec : SubscriptionSpec
            The subscription specification.
        """
        by_sync = self._queues[queue][sub_spec.channel_filter.sync]
        by_sync[sub_spec.data_type_name].discard(sub_spec)

    async def auth_read(self, hostname, username, data_type, *,
                        user_address=None):
        '''Get DBR data and native data, converted to a specific type'''
        access = self.check_access(hostname, username)
        if AccessRights.READ not in access:
            raise Forbidden("Client with hostname {} and username {} cannot "
                            "read.".format(hostname, username))
        return (await self.read(data_type))

    async def read(self, data_type):
        """
        Read out the ChannelData as ``data_type``.

        Parameters
        ----------
        data_type : ChannelType
            The data type to read out.
        """
        # Subclass might trigger a write here to update self._data before
        # reading it out.
        return (await self._read(data_type))

    async def _read(self, data_type):
        """
        Inner method to read out the ChannelData as ``data_type``.

        Parameters
        ----------
        data_type : ChannelType
            The data type to read out.
        """
        # special cases for alarm strings and class name
        if data_type == ChannelType.STSACK_STRING:
            ret = await self.alarm.read()
            return (ret, b'')
        elif data_type == ChannelType.CLASS_NAME:
            class_name = DBR_TYPES[data_type]()
            rtyp = self.reported_record_type.encode(self.string_encoding)
            class_name.value = rtyp
            return class_name, b''

        if data_type in _LongStringChannelType:
            native_to = _LongStringChannelType.LONG_STRING
            data_type = ChannelType(data_type)
        else:
            native_to = native_type(data_type)

        values = backend.convert_values(
            values=self._data['value'],
            from_dtype=self.data_type,
            to_dtype=native_to,
            string_encoding=self.string_encoding,
            enum_strings=self._data.get('enum_strings'),
            direction=ConversionDirection.TO_WIRE,
        )

        # for native types, there is no dbr metadata - just data
        if data_type in native_types:
            return b'', values

        dbr_metadata = DBR_TYPES[data_type]()
        self._read_metadata(dbr_metadata)

        # Copy alarm fields also.
        alarm_dbr = await self.alarm.read()
        for field, _ in alarm_dbr._fields_:
            if hasattr(dbr_metadata, field):
                setattr(dbr_metadata, field, getattr(alarm_dbr, field))

        return dbr_metadata, values

    async def auth_write(self, hostname, username, data, data_type, metadata,
                         *, flags=0, user_address=None):
        """
        Data write hook for clients.

        First verifies that the specified client may write to the instance
        prior to proceeding.

        Parameters
        ----------
        hostname : str
            The hostname of the client.
        username : str
            The username of the client.
        data :
            The data to write.
        data_type : ChannelType
            The data type associated with the written data.
        metadata :
            Metadata instance.
        flags : int, optional
            Flags for publishing.
        user_address : tuple, optional
            (host, port) tuple of the user.

        Raises
        ------
        Forbidden:
            If client is not allowed to write to this instance.
        """
        access = self.check_access(hostname, username)
        if AccessRights.WRITE not in access:
            raise Forbidden("Client with hostname {} and username {} cannot "
                            "write.".format(hostname, username))
        return (await self.write_from_dbr(data, data_type, metadata,
                                          flags=flags))

    async def verify_value(self, data):
        """
        Verify a value prior to it being written by CA or Python

        To reject a value, raise an exception. Otherwise, return the
        original value or a modified version of it.
        """
        return data

    async def write_from_dbr(self, data, data_type, metadata, *, flags=0):
        """
        Write data from a provided DBR data type.

        Does not verify the client has authorization to write.

        Parameters
        ----------
        data :
            The data to write.
        data_type : ChannelType
            The data type associated with the written data.
        metadata :
            Metadata instance.
        flags : int, optional
            Flags for publishing.

        Raises
        ------
        CaprotoValueError:
            If the request is not valid.
        """
        if data_type == ChannelType.PUT_ACKS:
            await self.alarm.write(severity_to_acknowledge=metadata.value)
            return
        elif data_type == ChannelType.PUT_ACKT:
            await self.alarm.write(must_acknowledge_transient=metadata.value)
            return
        elif data_type in (ChannelType.STSACK_STRING, ChannelType.CLASS_NAME):
            raise CaprotoValueError('Bad request')

        timestamp = time.time()
        native_from = native_type(data_type)
        value = backend.convert_values(
            values=data, from_dtype=native_from,
            to_dtype=self.data_type,
            string_encoding=self.string_encoding,
            enum_strings=getattr(self, 'enum_strings', None),
            direction=ConversionDirection.FROM_WIRE)

        if metadata is None:
            metadata_dict = {}
        else:
            # Convert `metadata` to bytes-like (or pass it through).
            md_payload = parse_metadata(metadata, data_type)

            # Depending on the type of `metadata` above,
            # `md_payload` could be a DBR struct or plain bytes.
            # Load it into a struct (zero-copy) to be sure.
            dbr_metadata = DBR_TYPES[data_type].from_buffer(md_payload)
            metadata_dict = dbr_metadata_to_dict(dbr_metadata,
                                                 self.string_encoding)
            metadata_dict.setdefault('timestamp', timestamp)

        return (await self.write(value, flags=flags, **metadata_dict))

    async def write(self, value, *, flags=0, verify_value=True,
                    update_fields=True, **metadata):
        """
        Write data from native Python types.

        Metadata may be updated at the same time by way of keyword arguments.
        Refer to the parameters section below for the keywords or the method
        ``write_metadata``.

        Parameters
        ----------
        value :
            The native Python data.
        flags : SubscriptionType or int, optional
            The flags for subscribers.
        verify_value : bool, optional
            Run the ``verify_value`` hook prior to updating internal state.
        update_fields : bool, optional
            Run the ``update_fields`` hook prior to updating internal state.
        units : str, optional
            [Metadata] Updated units.
        precision : int, optional
            [Metadata] Updated precision value.
        timestamp : float, TimeStamp, or 2-tuple, optional
            [Metadata] Updated timestamp. Supported options include
            ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the
            form of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
        upper_disp_limit : int or float, optional
            [Metadata] Updated upper display limit.
        lower_disp_limit : int or float, optional
            [Metadata] Updated lower display limit.
        upper_alarm_limit : int or float, optional
            [Metadata] Updated upper alarm limit.
        upper_warning_limit : int or float, optional
            [Metadata] Updated upper warning limit.
        lower_warning_limit : int or float, optional
            [Metadata] Updated lower warning limit.
        lower_alarm_limit : int or float, optional
            [Metadata] Updated lower alarm limit.
        upper_ctrl_limit : int or float, optional
            [Metadata] Updated upper control limit.
        lower_ctrl_limit : int or float, optional
            [Metadata] Updated lower control limit.
        status : AlarmStatus, optional
            [Metadata] Updated alarm status.
        severity : AlarmSeverity, optional
            [Metadata] Updated alarm severity.

        Raises
        ------
        Exception:
            Any exception raised in the handlers (except SkipWrite) will be
            propagated to the caller.
        """

        try:
            value = self.preprocess_value(value)
            if verify_value:
                modified_value = await self.verify_value(value)
            else:
                modified_value = None

            if update_fields:
                await self.update_fields(
                    modified_value if verify_value else value
                )
        except SkipWrite:
            # Handler raised SkipWrite to avoid the rest of this method.
            return
        except GeneratorExit:
            raise
        except Exception:
            # TODO: should allow exception to optionally pass alarm
            # status/severity through exception instance
            await self.alarm.write(status=AlarmStatus.WRITE,
                                   severity=AlarmSeverity.MAJOR_ALARM,
                                   )
            raise
        finally:
            alarm_md = self._collect_alarm()

        if modified_value is SkipWrite:
            # An alternative to raising SkipWrite: avoid the rest of this
            # method.
            return

        # issues of over-riding user passed in data here!
        metadata.update(alarm_md)
        metadata.setdefault('timestamp', time.time())

        if self._fill_at_next_write:
            snapshot = copy.deepcopy(self)
            for state, mode in self._fill_at_next_write:
                self._snapshots[state][mode] = snapshot
            self._fill_at_next_write.clear()

        new = modified_value if modified_value is not None else value

        # TODO the next 5 lines should be done in one move
        self._data['value'] = new
        await self.write_metadata(publish=False, **metadata)
        # Send a new event to subscribers.
        await self.publish(flags)
        # and publish any linked alarms
        if 'status' in metadata or 'severity' in metadata:
            await self.alarm.publish(flags, except_for=(self,))

    def _is_eligible(self, ss):
        sync = ss.channel_filter.sync
        return sync is None or sync.m in self._snapshots[sync.s]

    async def update_fields(self, value):
        """This is a hook for subclasses to update field instance data."""

    async def publish(self, flags):
        """
        Publish data to appropriate queues matching the SubscriptionSpec.

        Each SubscriptionSpec specifies a certain data type it is interested in
        and a mask. Send one update per queue per data_type if and only if any
        subscriptions specs on a queue have a compatible mask.

        Parameters
        ----------
        flags : SubscriptionSpec
            The subscription specification to match.
        """
        # Copying the data into structs with various data types is expensive,
        # so we only want to do it if it's going to be used, and we only want
        # to do each conversion once. Clear the cache to start. This cache is
        # instance state so that self.subscribe can also use it.
        self._content.clear()

        for queue, syncs in self._queues.items():
            # queue belongs to a Context that is expecting to receive
            # updates of the form (sub_specs, metadata, values).
            # data_types is a dict grouping the sub_specs for this queue by
            # their data_type.
            for sync, data_types in syncs.items():
                for data_type_name, sub_specs in data_types.items():
                    eligible = tuple(ss for ss in sub_specs
                                     if self._is_eligible(ss))
                    if not eligible:
                        continue
                    if sync is None:
                        channel_data = self
                    else:
                        try:
                            channel_data = self._snapshots[sync.s][sync.m]
                        except KeyError:
                            continue
                    try:
                        metdata, values = self._content[data_type_name]
                    except KeyError:
                        # Do the expensive data type conversion and cache it in
                        # case another queue or a future subscription wants the
                        # same data type.
                        data_type = _channel_type_by_name[data_type_name]
                        metadata, values = await channel_data._read(data_type)
                        channel_data._content[data_type] = metadata, values

                    # We will apply the array filter and deadband on the other side
                    # of the queue, since each eligible SubscriptionSpec may
                    # want a different slice. Sending the whole array through
                    # the queue isn't any more expensive that sending a slice;
                    # this is just a reference.
                    await queue.put(SubscriptionUpdate(eligible, metadata, values, flags, None))

    def _read_metadata(self, dbr_metadata):
        """Fill the provided metadata instance with current metadata."""
        to_type = ChannelType(dbr_metadata.DBR_ID)
        data = self._data

        if hasattr(dbr_metadata, 'units'):
            units = data.get('units', '')
            if isinstance(units, str):
                units = units.encode(self.string_encoding
                                     if self.string_encoding
                                     else 'latin-1')
            dbr_metadata.units = units

        if hasattr(dbr_metadata, 'precision'):
            dbr_metadata.precision = data.get('precision', 0)

        if to_type in time_types:
            dbr_metadata.stamp = self.epics_timestamp

        convert_attrs = (GraphicControlBase.control_fields +
                         GraphicControlBase.graphic_fields)

        if any(hasattr(dbr_metadata, attr) for attr in convert_attrs):
            # convert all metadata types to the target type
            dt = (self.data_type
                  if self.data_type != ChannelType.ENUM
                  else ChannelType.INT)
            values = backend.convert_values(
                values=[data.get(key, 0) for key in convert_attrs],
                from_dtype=dt,
                to_dtype=native_type(to_type),
                string_encoding=self.string_encoding,
                direction=ConversionDirection.TO_WIRE,
                auto_byteswap=False)
            if isinstance(values, backend.array_types):
                values = values.tolist()
            for attr, value in zip(convert_attrs, values):
                if hasattr(dbr_metadata, attr):
                    setattr(dbr_metadata, attr, value)

    async def write_metadata(self, publish=True, units=None, precision=None,
                             timestamp=None, upper_disp_limit=None,
                             lower_disp_limit=None, upper_alarm_limit=None,
                             upper_warning_limit=None,
                             lower_warning_limit=None, lower_alarm_limit=None,
                             upper_ctrl_limit=None, lower_ctrl_limit=None,
                             status=None, severity=None):
        """
        Write metadata, optionally publishing information to clients.

        Parameters
        ----------
        publish : bool, optional
            Publish the metadata update to clients.
        units : str, optional
            Updated units.
        precision : int, optional
            Updated precision value.
        timestamp : float, TimeStamp, or 2-tuple, optional
            Updated timestamp. Supported options include ``time.time()``-style
            UNIX timestamps, raw EPICS timestamps in the form of ``TimeStamp``
            or ``(seconds_since_epoch, nanoseconds)``.
        upper_disp_limit : int or float, optional
            Updated upper display limit.
        lower_disp_limit : int or float, optional
            Updated lower display limit.
        upper_alarm_limit : int or float, optional
            Updated upper alarm limit.
        upper_warning_limit : int or float, optional
            Updated upper warning limit.
        lower_warning_limit : int or float, optional
            Updated lower warning limit.
        lower_alarm_limit : int or float, optional
            Updated lower alarm limit.
        upper_ctrl_limit : int or float, optional
            Updated upper control limit.
        lower_ctrl_limit : int or float, optional
            Updated lower control limit.
        status : AlarmStatus, optional
            Updated alarm status.
        severity : AlarmSeverity, optional
            Updated alarm severity.
        """
        data = self._data
        for kw in ('units', 'precision', 'upper_disp_limit',
                   'lower_disp_limit', 'upper_alarm_limit',
                   'upper_warning_limit', 'lower_warning_limit',
                   'lower_alarm_limit', 'upper_ctrl_limit',
                   'lower_ctrl_limit'):
            value = locals()[kw]
            if value is not None and kw in data:
                # Unpack scalars. This could be skipped for numpy.ndarray which
                # does the right thing, but is essential for array.array to
                # work.
                try:
                    value, = value
                except (TypeError, ValueError):
                    pass
                data[kw] = value

        if timestamp is not None:
            self._data["timestamp"] = TimeStamp.from_flexible_value(timestamp)

        if status is not None or severity is not None:
            await self.alarm.write(status=status, severity=severity,
                                   publish=publish)

        if publish:
            await self.publish(SubscriptionType.DBE_PROPERTY)

    @property
    def timestamp(self) -> float:
        """UNIX timestamp in seconds."""
        return self._data["timestamp"].timestamp

    @property
    def epics_timestamp(self) -> TimeStamp:
        """EPICS timestamp as (seconds, nanoseconds) since EPICS epoch."""
        return copy.copy(self._data["timestamp"])

    @property
    def status(self):
        '''Alarm status'''
        return (self.alarm.status
                if self._status is None
                else self._status)

    @status.setter
    def status(self, value):
        self._status = AlarmStatus(value)

    @property
    def severity(self):
        '''Alarm severity'''
        return (self.alarm.severity
                if self._severity is None
                else self._severity)

    @severity.setter
    def severity(self, value):
        self._severity = AlarmSeverity(value)

    def _collect_alarm(self):
        out = {}
        if self._status is not None and self._status != self.alarm.status:
            out['status'] = self._status
        if self._severity is not None and self._status != self.alarm.status:
            out['severity'] = self._severity

        self._clear_cached_alarms()
        return out

    def _clear_cached_alarms(self):
        self._status = self._severity = None

    def __len__(self):
        try:
            return len(self.value)
        except TypeError:
            return 1

    def check_access(self, hostname, username):
        """
        This always returns ``AccessRights.READ|AccessRights.WRITE``.

        Subclasses can override to implement access logic using hostname,
        username and returning one of:
        (``AccessRights.NO_ACCESS``,
         ``AccessRights.READ``,
         ``AccessRights.WRITE``,
         ``AccessRights.READ|AccessRights.WRITE``).

        Parameters
        ----------
        hostname : string
        username : string

        Returns
        -------
        access : :data:`AccessRights.READ|AccessRights.WRITE`
        """
        return AccessRights.READ | AccessRights.WRITE

    def is_compatible_array(self, value) -> bool:
        """
        Check if the provided value is a compatible array.

        This requires that ``value`` follow the "array interface", as defined by
        `numpy <https://numpy.org/doc/stable/reference/arrays.interface.html>`_.

        Parameters
        ----------
        value : any
            The value to check.

        Returns
        -------
        bool
            True if ``value`` is compatible, False otherwise.
        """
        interface = getattr(value, "__array_interface__", None)
        if interface is None:
            return False

        dimensions = len(interface['shape'])
        return (
            # Ensure it's 1 dimensional:
            dimensions == 1 and
            # Not strided - which 1D data shouldn't be anyway...
            interface['strides'] is None and
            # And a compatible array type, defined in the class body:
            interface['typestr'] in self._compatible_array_types
        )


class ChannelEnum(ChannelData):
    """
    ENUM data which can be sent over a channel.

    Arrays of ENUM data are not supported.

    Parameters
    ----------
    value : int or str
        The string value in the enum, or an integer index of that list.
    enum_strings : list, tuple, optional
        Enum strings to be used for the data.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    """

    data_type = ChannelType.ENUM

    @staticmethod
    def _validate_enum_strings(enum_strings):
        if any(len(es) >= MAX_ENUM_STRING_SIZE for es in enum_strings):
            over_length = tuple(f'{es}: {len(es)}' for es in enum_strings if
                                len(es) >= MAX_ENUM_STRING_SIZE)
            msg = (f"The maximum enum string length is {MAX_ENUM_STRING_SIZE} " +
                   f"but the strings {over_length} are too long")
            raise ValueError(msg)
        if len(enum_strings) > MAX_ENUM_STATES:
            raise ValueError(f"The maximum number of enum states is {MAX_ENUM_STATES} " +
                             f"but you passed in {len(enum_strings)}")
        return tuple(enum_strings)

    def __init__(self, *, enum_strings=None, **kwargs):
        super().__init__(**kwargs)

        if enum_strings is None:
            enum_strings = tuple()
        self._data['enum_strings'] = self._validate_enum_strings(enum_strings)

    enum_strings = _read_only_property('enum_strings')

    def get_raw_value(self, value) -> int:
        """The raw integer value index of the provided enum string."""
        try:
            return self.enum_strings.index(value)
        except ValueError:
            return None

    @property
    def raw_value(self) -> int:
        """The raw integer value index of the enum string."""
        return self.get_raw_value(self.value)

    def __getnewargs_ex__(self):
        args, kwargs = super().__getnewargs_ex__()
        kwargs['enum_strings'] = self.enum_strings
        return (args, kwargs)

    async def verify_value(self, data):
        try:
            return self.enum_strings[data]
        except (IndexError, TypeError):
            ...
        return data

    def _read_metadata(self, dbr_metadata):
        if isinstance(dbr_metadata, (DBR_TYPES[ChannelType.GR_ENUM],
                                     DBR_TYPES[ChannelType.CTRL_ENUM])):
            dbr_metadata.enum_strings = [s.encode(self.string_encoding)
                                         for s in self.enum_strings]

        return super()._read_metadata(dbr_metadata)

    async def write(self, *args, flags=0, **kwargs):
        flags |= (SubscriptionType.DBE_LOG | SubscriptionType.DBE_VALUE)
        await super().write(*args, flags=flags, **kwargs)

    async def write_from_dbr(self, *args, flags=0, **kwargs):
        flags |= (SubscriptionType.DBE_LOG | SubscriptionType.DBE_VALUE)
        await super().write_from_dbr(*args, flags=flags, **kwargs)

    async def write_metadata(self, enum_strings=None, **kwargs):
        if enum_strings is not None:
            self._data['enum_strings'] = self._validate_enum_strings(enum_strings)

        return await super().write_metadata(**kwargs)


class ChannelNumeric(ChannelData):
    """
    Base class for numeric data types with limits.

    May be a single value or an array of values.

    Parameters
    ----------
    value :
        Data which has to match with this class's ``data_type``.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual
        record or be set to something arbitrary.  Defaults to 'caproto'.
    units : str, optional
        Engineering units indicator, which can be retrieved over channel
        access.
    upper_disp_limit : numeric, optional
        Upper display limit.
    lower_disp_limit : numeric, optional
        Lower display limit.
    upper_alarm_limit : numeric, optional
        Upper alarm limit.
    upper_warning_limit : numeric, optional
        Upper warning limit.
    lower_warning_limit : numeric, optional
        Lower warning limit.
    lower_alarm_limit : numeric, optional
        Lower alarm limit.
    upper_ctrl_limit : numeric, optional
        Upper control limit.
    lower_ctrl_limit : numeric, optional
        Lower control limit.
    value_atol : numeric, optional
        Archive tolerance value.
    log_atol : numeric, optional
        Log tolerance value.
    """

    def __init__(self, *, value, units='',
                 upper_disp_limit=0, lower_disp_limit=0,
                 upper_alarm_limit=0, upper_warning_limit=0,
                 lower_warning_limit=0, lower_alarm_limit=0,
                 upper_ctrl_limit=0, lower_ctrl_limit=0,
                 value_atol=0.0, log_atol=0.0,
                 **kwargs):
        super().__init__(value=value, **kwargs)
        self._data['units'] = units
        self._data['upper_disp_limit'] = upper_disp_limit
        self._data['lower_disp_limit'] = lower_disp_limit
        self._data['upper_alarm_limit'] = upper_alarm_limit
        self._data['upper_warning_limit'] = upper_warning_limit
        self._data['lower_warning_limit'] = lower_warning_limit
        self._data['lower_alarm_limit'] = lower_alarm_limit
        self._data['upper_ctrl_limit'] = upper_ctrl_limit
        self._data['lower_ctrl_limit'] = lower_ctrl_limit
        self.value_atol = value_atol
        self.log_atol = log_atol

    units = _read_only_property('units')

    upper_disp_limit = _read_only_property('upper_disp_limit')
    lower_disp_limit = _read_only_property('lower_disp_limit')

    upper_alarm_limit = _read_only_property('upper_alarm_limit')
    lower_alarm_limit = _read_only_property('lower_alarm_limit')

    upper_warning_limit = _read_only_property('upper_warning_limit')
    lower_warning_limit = _read_only_property('lower_warning_limit')

    upper_ctrl_limit = _read_only_property('upper_ctrl_limit')
    lower_ctrl_limit = _read_only_property('lower_ctrl_limit')

    def __getnewargs_ex__(self):
        args, kwargs = super().__getnewargs_ex__()
        kwargs.update(
            units=self.units,
            upper_disp_limit=self.upper_disp_limit,
            lower_disp_limit=self.lower_disp_limit,
            upper_alarm_limit=self.upper_alarm_limit,
            lower_alarm_limit=self.lower_alarm_limit,
            upper_warning_limit=self.upper_warning_limit,
            lower_warning_limit=self.lower_warning_limit,
            upper_ctrl_limit=self.upper_ctrl_limit,
            lower_ctrl_limit=self.lower_ctrl_limit,
        )
        return (args, kwargs)

    async def verify_value(self, data):
        if not isinstance(data, Iterable):
            val = data
        elif len(data) == 1:
            val, = data
        else:
            # data is an array -- limits do not apply.
            return data
        if self.lower_ctrl_limit != self.upper_ctrl_limit:
            if not self.lower_ctrl_limit <= val <= self.upper_ctrl_limit:
                raise CannotExceedLimits(
                    f"Cannot write data {val}. Limits are set to "
                    f"{self.lower_ctrl_limit} and {self.upper_ctrl_limit}."
                )

        def limit_checker(
                value,
                lo_attr, hi_attr,
                lo_status, hi_status,
                lo_severity_attr,
                hi_severity_attr,
                dflt_lo_severity,
                dflt_hi_severity):

            def limit_getter(limit_attr, severity_attr, dflt_severity):
                sev = dflt_severity
                limit = getattr(self, limit_attr)

                sev_prop = getattr(
                    getattr(self, 'field_inst', None),
                    severity_attr, None)
                if sev_prop is not None:
                    # TODO sort out where ints are getting through...
                    if isinstance(sev_prop.value, str):
                        sev = sev_prop.enum_strings.index(sev_prop.value)

                return limit, AlarmSeverity(sev)

            lo_limit, lo_severity = limit_getter(
                lo_attr, lo_severity_attr, dflt_lo_severity)
            hi_limit, hi_severity = limit_getter(
                hi_attr, hi_severity_attr, dflt_hi_severity)
            if lo_limit != hi_limit:
                if value <= lo_limit:
                    return lo_status, lo_severity

                elif hi_limit <= value:
                    return hi_status, hi_severity

            return AlarmStatus.NO_ALARM, AlarmSeverity.NO_ALARM

        # this is HIHI and LOLO limits
        asts, asver = limit_checker(val,
                                    'lower_alarm_limit',
                                    'upper_alarm_limit',
                                    AlarmStatus.LOLO,
                                    AlarmStatus.HIHI,
                                    'lolo_severity',
                                    'hihi_severity',
                                    AlarmSeverity.MAJOR_ALARM,
                                    AlarmSeverity.MAJOR_ALARM)
        # if HIHI and LOLO did not trigger as alarm, see if HIGH and LOW do
        if asts is AlarmStatus.NO_ALARM:
            # this is HIGH and LOW limits
            asts, asver = limit_checker(val,
                                        'lower_warning_limit',
                                        'upper_warning_limit',
                                        AlarmStatus.LOW,
                                        AlarmStatus.HIGH,
                                        'low_severity',
                                        'high_severity',
                                        AlarmSeverity.MINOR_ALARM,
                                        AlarmSeverity.MINOR_ALARM)

        self.status = asts
        self.severity = asver

        return data


class ChannelShort(ChannelNumeric):
    """
    16-bit SHORT integer data and metadata which can be sent over a channel.

    May be a single value or an array of values.

    Parameters
    ----------
    value : int or list of int
        Default starting value.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual
        record or be set to something arbitrary.  Defaults to 'caproto'.
    units : str, optional
        Engineering units indicator, which can be retrieved over channel
        access.
    upper_disp_limit : int, optional
        Upper display limit.
    lower_disp_limit : int, optional
        Lower display limit.
    upper_alarm_limit : int, optional
        Upper alarm limit.
    upper_warning_limit : int, optional
        Upper warning limit.
    lower_warning_limit : int, optional
        Lower warning limit.
    lower_alarm_limit : int, optional
        Lower alarm limit.
    upper_ctrl_limit : int, optional
        Upper control limit.
    lower_ctrl_limit : int, optional
        Lower control limit.
    value_atol : int, optional
        Archive tolerance value.
    log_atol : int, optional
        Log tolerance value.
    """
    data_type = ChannelType.INT


class ChannelInteger(ChannelNumeric):
    """
    32-bit LONG integer data and metadata which can be sent over a channel.

    May be a single value or an array of values.

    Parameters
    ----------
    value : int or list of int
        Default starting value.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual
        record or be set to something arbitrary.  Defaults to 'caproto'.
    units : str, optional
        Engineering units indicator, which can be retrieved over channel
        access.
    upper_disp_limit : int, optional
        Upper display limit.
    lower_disp_limit : int, optional
        Lower display limit.
    upper_alarm_limit : int, optional
        Upper alarm limit.
    upper_warning_limit : int, optional
        Upper warning limit.
    lower_warning_limit : int, optional
        Lower warning limit.
    lower_alarm_limit : int, optional
        Lower alarm limit.
    upper_ctrl_limit : int, optional
        Upper control limit.
    lower_ctrl_limit : int, optional
        Lower control limit.
    value_atol : int, optional
        Archive tolerance value.
    log_atol : int, optional
        Log tolerance value.
    """
    data_type = ChannelType.LONG


class ChannelFloat(ChannelNumeric):
    """
    32-bit floating point data and metadata which can be sent over a channel.

    May be a single value or an array of values.

    Parameters
    ----------
    value : float or list of float
        Initial value for the data.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual record or
        be set to something arbitrary.  Defaults to 'caproto'.
    units : str, optional
        Engineering units indicator, which can be retrieved over channel
        access.
    upper_disp_limit : float, optional
        Upper display limit.
    lower_disp_limit : float, optional
        Lower display limit.
    upper_alarm_limit : float, optional
        Upper alarm limit.
    upper_warning_limit : float, optional
        Upper warning limit.
    lower_warning_limit : float, optional
        Lower warning limit.
    lower_alarm_limit : float, optional
        Lower alarm limit.
    upper_ctrl_limit : float, optional
        Upper control limit.
    lower_ctrl_limit : float, optional
        Lower control limit.
    value_atol : float, optional
        Archive tolerance value.
    log_atol : float, optional
        Log tolerance value.
    """

    data_type = ChannelType.FLOAT

    def __init__(self, *, precision=0, **kwargs):
        super().__init__(**kwargs)
        self._data['precision'] = precision

    precision = _read_only_property('precision')

    def __getnewargs_ex__(self):
        args, kwargs = super().__getnewargs_ex__()
        kwargs['precision'] = self.precision
        return (args, kwargs)


class ChannelDouble(ChannelNumeric):
    """
    64-bit floating point data and metadata which can be sent over a channel.

    May be a single value or an array of values.

    Parameters
    ----------
    value : float or list of float
        Initial value for the data.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual
        record or be set to something arbitrary.  Defaults to 'caproto'.
    units : str, optional
        Engineering units indicator, which can be retrieved over channel
        access.
    upper_disp_limit : float, optional
        Upper display limit.
    lower_disp_limit : float, optional
        Lower display limit.
    upper_alarm_limit : float, optional
        Upper alarm limit.
    upper_warning_limit : float, optional
        Upper warning limit.
    lower_warning_limit : float, optional
        Lower warning limit.
    lower_alarm_limit : float, optional
        Lower alarm limit.
    upper_ctrl_limit : float, optional
        Upper control limit.
    lower_ctrl_limit : float, optional
        Lower control limit.
    value_atol : float, optional
        Archive tolerance value.
    log_atol : float, optional
        Log tolerance value.
    """
    data_type = ChannelType.DOUBLE

    def __init__(self, *, precision=0, **kwargs):
        super().__init__(**kwargs)

        self._data['precision'] = precision

    precision = _read_only_property('precision')

    def __getnewargs_ex__(self):
        args, kwargs = super().__getnewargs_ex__()
        kwargs['precision'] = self.precision
        return (args, kwargs)


class ChannelByte(ChannelNumeric):
    """
    8-bit unencoded CHAR data plus metadata which can be sent over a channel.

    May be a single CHAR or an array of CHAR.

    Parameters
    ----------
    value : int or bytes
        Initial starting data.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual
        record or be set to something arbitrary.  Defaults to 'caproto'.
    units : str, optional
        Engineering units indicator, which can be retrieved over channel
        access.
    upper_disp_limit : int, optional
        Upper display limit.
    lower_disp_limit : int, optional
        Lower display limit.
    upper_alarm_limit : int, optional
        Upper alarm limit.
    upper_warning_limit : int, optional
        Upper warning limit.
    lower_warning_limit : int, optional
        Lower warning limit.
    lower_alarm_limit : int, optional
        Lower alarm limit.
    upper_ctrl_limit : int, optional
        Upper control limit.
    lower_ctrl_limit : int, optional
        Lower control limit.
    value_atol : int, optional
        Archive tolerance value.
    log_atol : int, optional
        Log tolerance value.
    """

    # 'Limits' on chars do not make much sense and are rarely used.
    data_type = ChannelType.CHAR
    _compatible_array_types = {'|u1', '|i1', '|b1'}

    def __init__(self, *, string_encoding=None, strip_null_terminator=True,
                 **kwargs):
        if string_encoding is not None:
            raise CaprotoValueError('ChannelByte cannot have a string encoding')

        self.strip_null_terminator = strip_null_terminator
        super().__init__(string_encoding=None, **kwargs)

    def preprocess_value(self, value):
        value = super().preprocess_value(value)

        if self.is_compatible_array(value):
            if not is_array_read_only(value):
                value = copy.copy(value)
            if self.max_length == 1:
                return value[0]
            return value

        if isinstance(value, (list, tuple) + backend.array_types):
            if not len(value):
                return b''
            elif len(value) == 1:
                value = value[0]
            else:
                value = b''.join(map(bytes, ([v] for v in value)))

        if self.max_length == 1:
            try:
                len(value)
            except TypeError:
                # Allow a scalar byte value to be passed in
                value = bytes([value])

        if isinstance(value, str):
            raise CaprotoValueError('ChannelByte does not accept decoded strings')

        if self.strip_null_terminator:
            if not isinstance(value, bytes):
                value = value.tobytes()
            value = value.rstrip(b'\x00')

        return value


class ChannelChar(ChannelData):
    """
    8-bit encoded CHAR data plus metadata which can be sent over a channel.

    Allows for simple access over CA to the first 40 characters as a DBR_STRING
    when ``report_as_string`` is set.

    Parameters
    ----------
    value : str
        Initial starting data.
    string_encoding : str, optional
        The string encoding to use, defaults to 'latin-1'.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual
        record or be set to something arbitrary.  Defaults to 'caproto'.
    units : str, optional
        Engineering units indicator, which can be retrieved over channel
        access.
    """
    data_type = ChannelType.CHAR
    _compatible_array_types = {'|u1', '|i1', '|b1'}

    def __init__(self, *, alarm=None, value=None, timestamp=None,
                 max_length=None, string_encoding='latin-1',
                 reported_record_type='caproto', report_as_string=False):
        super().__init__(alarm=alarm, value=value, timestamp=timestamp,
                         max_length=max_length,
                         string_encoding=string_encoding,
                         reported_record_type=reported_record_type)

        if report_as_string:
            self.data_type = ChannelType.STRING

    @property
    def long_string_max_length(self):
        'The maximum number of elements (length) of the current value'
        return super().max_length

    @property
    def max_length(self):
        'The number of elements (length) of the current value'
        if self.data_type == ChannelType.STRING:
            return 1
        return super().max_length

    def preprocess_value(self, value):
        value = super().preprocess_value(value)

        if self.is_compatible_array(value):
            value = value.tobytes()
        elif isinstance(value, (list, tuple) + backend.array_types):
            if not len(value):
                value = b''
            elif len(value) == 1:
                value = value[0]
            else:
                value = b''.join(map(bytes, ([v] for v in value)))

        if self.max_length == 1:
            try:
                len(value)
            except TypeError:
                # Allow a scalar byte value to be passed in
                value = str(bytes([value]), self.string_encoding)

        if isinstance(value, bytes):
            value = value.decode(self.string_encoding)

        if not isinstance(value, str):
            raise CaprotoValueError('Invalid string')

        return value

    async def write(self, *args, flags=0, **kwargs):
        flags |= (SubscriptionType.DBE_LOG | SubscriptionType.DBE_VALUE)
        await super().write(*args, flags=flags, **kwargs)

    async def write_from_dbr(self, *args, flags=0, **kwargs):
        flags |= (SubscriptionType.DBE_LOG | SubscriptionType.DBE_VALUE)
        await super().write_from_dbr(*args, flags=flags, **kwargs)


class ChannelString(ChannelData):
    """
    8-bit encoded string data plus metadata which can be sent over a channel.

    Allows for simple access over CA to the first 40 characters as a DBR_STRING
    when ``report_as_string`` is set.

    Parameters
    ----------
    value : str
        Initial starting data.
    string_encoding : str, optional
        The string encoding to use, defaults to 'latin-1'.
    long_string_max_length : str, optional
        When requested as a long string (DBR_CHAR over Channel Access), this
        is the reported maximum length.
    timestamp : float, TimeStamp, or 2-tuple, optional
        Timestamp to report for the current value. Supported options include
        ``time.time()``-style UNIX timestamps, raw EPICS timestamps in the form
        of ``TimeStamp`` or ``(seconds_since_epoch, nanoseconds)``.
    max_length : int, optional
        Maximum array length of the data.
    string_encoding : str, optional
        Encoding to use for strings, used when serializing and deserializing
        data.
    reported_record_type : str, optional
        Though this is not a record, the channel access protocol supports
        querying the record type.  This can be set to mimic an actual
        record or be set to something arbitrary.  Defaults to 'caproto'.
    """
    data_type = ChannelType.STRING

    def __init__(self, *, alarm=None, value=None, timestamp=None,
                 max_length=None, string_encoding='latin-1',
                 reported_record_type='caproto', long_string_max_length=81):
        super().__init__(alarm=alarm, value=value, timestamp=timestamp,
                         max_length=max_length,
                         string_encoding=string_encoding,
                         reported_record_type=reported_record_type)

        self._long_string_max_length = long_string_max_length

    @property
    def long_string_max_length(self):
        'The maximum number of elements (length) of the current value'
        return self._long_string_max_length

    async def write(self, value, *, flags=0, **metadata):
        flags |= (SubscriptionType.DBE_LOG | SubscriptionType.DBE_VALUE)
        await super().write(value, flags=flags, **metadata)

    async def write_from_dbr(self, *args, flags=0, **kwargs):
        flags |= (SubscriptionType.DBE_LOG | SubscriptionType.DBE_VALUE)
        await super().write_from_dbr(*args, flags=flags, **kwargs)
