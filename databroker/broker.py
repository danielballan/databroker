from __future__ import print_function
import warnings
import six  # noqa
import traceback
import uuid
from datetime import datetime
import pytz
import logging
import numbers
import requests
import time
from doct import Document
from .core import (Header,
                   get_events as _get_events,
                   get_table as _get_table,
                   restream as _restream,
                   fill_event as _fill_event,
                   process as _process, Images,
                   get_fields,  # for conveniece
                   ALL
                  )


def _format_time(search_dict, tz):
    """Helper function to format the time arguments in a search dict

    Expects 'start_time' and 'stop_time'

    ..warning: Does in-place mutation of the search_dict
    """
    time_dict = {}
    start_time = search_dict.pop('start_time', None)
    stop_time = search_dict.pop('stop_time', None)
    if start_time:
        time_dict['$gte'] = _normalize_human_friendly_time(start_time, tz)
    if stop_time:
        time_dict['$lte'] = _normalize_human_friendly_time(stop_time, tz)
    if time_dict:
        search_dict['time'] = time_dict


def doc_or_uid_to_uid(doc_or_uid):
    """Given Document or uid return the uid

    Parameters
    ----------
    doc_or_uid : dict or str
        If str, then assume uid and pass through, if not, return
        the 'uid' field

    Returns
    -------
    uid : str
        A string version of the uid of the given document

    """
    if not isinstance(doc_or_uid, six.string_types):
        doc_or_uid = doc_or_uid['uid']
    return doc_or_uid


# human friendly timestamp formats we'll parse
_TS_FORMATS = [
    '%Y-%m-%d %H:%M:%S',
    '%Y-%m-%d %H:%M',  # these 2 are not as originally doc'd,
    '%Y-%m-%d %H',     # but match previous pandas behavior
    '%Y-%m-%d',
    '%Y-%m',
    '%Y']

# build a tab indented, '-' bulleted list of supported formats
# to append to the parsing function docstring below
_doc_ts_formats = '\n'.join('\t- {}'.format(_) for _ in _TS_FORMATS)


def _normalize_human_friendly_time(val, tz):
    """Given one of :
    - string (in one of the formats below)
    - datetime (eg. datetime.datetime.now()), with or without tzinfo)
    - timestamp (eg. time.time())
    return a timestamp (seconds since jan 1 1970 UTC).

    Non string/datetime.datetime values are returned unaltered.
    Leading/trailing whitespace is stripped.
    Supported formats:
    {}
    """
    # {} is placeholder for formats; filled in after def...

    zone = pytz.timezone(tz)  # tz as datetime.tzinfo object
    epoch = pytz.UTC.localize(datetime(1970, 1, 1))
    check = True

    if isinstance(val, six.string_types):
        # unix 'date' cmd format '%a %b %d %H:%M:%S %Z %Y' works but
        # doesn't get TZ?

        # Could cleanup input a bit? remove leading/trailing [ :,-]?
        # Yes, leading/trailing whitespace to match pandas behavior...
        # Actually, pandas doesn't ignore trailing space, it assumes
        # the *current* month/day if they're missing and there's
        # trailing space, or the month is a single, non zero-padded digit.?!
        val = val.strip()

        for fmt in _TS_FORMATS:
            try:
                ts = datetime.strptime(val, fmt)
                break
            except ValueError:
                pass

        try:
            if isinstance(ts, datetime):
                val = ts
                check = False
            else:
                # what else could the type be here?
                raise TypeError('expected datetime.datetime,'
                                ' got {:r}'.format(ts))

        except NameError:
            raise ValueError('failed to parse time: ' + repr(val))

    if check and not isinstance(val, datetime):
        return val

    if val.tzinfo is None:
        # is_dst=None raises NonExistent and Ambiguous TimeErrors
        # when appropriate, same as pandas
        val = zone.localize(val, is_dst=None)

    return (val - epoch).total_seconds()


# fill in the placeholder we left in the previous docstring
_normalize_human_friendly_time.__doc__ = (
    _normalize_human_friendly_time.__doc__.format(_doc_ts_formats)
)


# Toolz and CyToolz have identical APIs -- same test suite, docstrings.
try:
    from cytoolz.dicttoolz import merge
except ImportError:
    from toolz.dicttoolz import merge


try:
    from functools import singledispatch
except ImportError:
    try:
        # We are running on Python 2.6, 2.7, or 3.3
        from singledispatch import singledispatch
    except ImportError:
        raise ImportError(
            "Please install singledispatch from PyPI"
            "\n\n   pip install singledispatch"
            "\n\nThen run your program again."
        )
try:
    from collections.abc import MutableSequence
except ImportError:
    # This will error on python < 3.3
    from collections import MutableSequence


logger = logging.getLogger(__name__)


@singledispatch
def search(key, mds):
    logger.info('Using default search for key = %s' % key)
    raise ValueError("Must give an integer scan ID like [6], a slice "
                     "into past scans like [-5], [-5:], or [-5:-9:2], "
                     "a list like [1, 7, 13], a (partial) uid "
                     "like ['a23jslk'] or a full uid like "
                     "['f26efc1d-8263-46c8-a560-7bf73d2786e1'].")


@search.register(slice)
def _(key, mds):
    # Interpret key as a slice into previous scans.
    logger.info('Interpreting key = %s as a slice' % key)
    if key.start is not None and key.start > -1:
        raise ValueError("slice.start must be negative. You gave me "
                         "key=%s The offending part is key.start=%s"
                         % (key, key.start))
    if key.stop is not None and key.stop > 0:
        raise ValueError("slice.stop must be <= 0. You gave me key=%s. "
                         "The offending part is key.stop = %s"
                         % (key, key.stop))
    if key.stop is not None:
        stop = -key.stop
    else:
        stop = None
    if key.start is None:
        raise ValueError("slice.start cannot be None because we do not "
                         "support slicing infinitely into the past; "
                         "the size of the result is non-deterministic "
                         "and could become too large.")
    start = -key.start
    result = list(mds.find_last(start))[stop::key.step]
    header = [Header.from_run_start(mds, h) for h in result]
    return header


@search.register(numbers.Integral)
def _(key, mds):
    logger.info('Interpreting key = %s as an integer' % key)
    if key > -1:
        # Interpret key as a scan_id.
        gen = mds.find_run_starts(scan_id=key)
        try:
            result = next(gen)  # most recent match
        except StopIteration:
            raise ValueError("No such run found for key=%s which is "
                             "being interpreted as a scan id." % key)
        header = Header.from_run_start(mds, result)
    else:
        # Interpret key as the Nth last scan.
        gen = mds.find_last(-key)
        for i in range(-key):
            try:
                result = next(gen)
            except StopIteration:
                raise IndexError(
                    "There are only {0} runs.".format(i))
        header = Header.from_run_start(mds, result)
    return header


@search.register(str)
@search.register(six.text_type)
@search.register(six.string_types,)
def _(key, mds):
    logger.info('Interpreting key = %s as a str' % key)
    results = None
    if len(key) == 36:
        # Interpret key as a complete uid.
        # (Try this first, for performance.)
        logger.debug('Treating %s as a full uuid' % key)
        results = list(mds.find_run_starts(uid=key))
        logger.debug('%s runs found for key=%s treated as a full uuid'
                     % (len(results), key))
    if not results:
        # No dice? Try searching as if we have a partial uid.
        logger.debug('Treating %s as a partial uuid' % key)
        gen = mds.find_run_starts(uid={'$regex': '{0}.*'.format(key)})
        results = list(gen)
    if not results:
        # Still no dice? Bail out.
        raise ValueError("No such run found for key=%r" % key)
    if len(results) > 1:
        raise ValueError("key=%r matches %d runs. Provide "
                         "more characters." % (key, len(results)))
    result, = results
    header = Header.from_run_start(mds, result)
    return header


@search.register(set)
@search.register(tuple)
@search.register(MutableSequence)
def _(key, mds):
    logger.info('Interpreting key = {} as a set, tuple or MutableSequence'
                ''.format(key))
    return [search(k, mds) for k in key]


class Broker(object):
    def __init__(self, mds, fs, plugins=None, filters=None):
        """
        Unified interface to data sources

        Parameters
        ----------
        mds : metadatastore or metadataclient
        fs : filestore
        plugins : dict or None, optional
            mapping keyword argument name (string) to Plugin, an object
            that should implement ``get_events``
        filters : list
            list of mongo queries to be combined with query using '$and',
            acting as a filter to restrict the results
        """
        self.mds = mds
        self.fs = fs
        if plugins is None:
            plugins = {}
        self.plugins = plugins
        if filters is None:
            filters = []
        self.filters = filters
        self.aliases = {}

    ALL = ALL  # sentinel used as default value for `stream_name`

    def _format_time(self, val):
        "close over the timezone config"
        # modifies a query dict in place, remove keys 'start_time' and
        # 'stop_time' and adding $lte and/or $gte queries on 'time' key
        _format_time(val, self.mds.config['timezone'])

    @property
    def filters(self):
        return self._filters

    @filters.setter
    def filters(self, val):
        for elem in val:
            self._format_time(elem)
        self._filters = val

    def add_filter(self, **kwargs):
        """
        Add query to the list of 'filter' queries.

        Filter queries are combined with every given query using '$and',
        acting as a filter to restrict the results.

        ``Broker.add_filter(**kwargs)`` is just a convenient way to spell
        ``Broker.filters.append(dict(**kwargs))``.

        Example
        -------
        Filter all searches to restrict runs to a specific 'user'.
        >>> db.add_filter(user='Dan')

        See Also
        --------
        `Broker.add_filter`

        """
        self.filters.append(dict(**kwargs))

    def clear_filters(self, **kwargs):
        """
        Clear all 'filter' queries.

        Filter queries are combined with every given query using '$and',
        acting as a filter to restrict the results.

        ``Broker.clear_filters()`` is just a convenient way to spell
        ``Broker.filters.clear()``.

        See Also
        --------
        `Broker.add_filter`
        """
        self.filters.clear()

    def __getitem__(self, key):
        """Do-What-I-Mean slicing"""
        return search(key, self.mds)

    def __getattr__(self, key):
        try:
            query = self.aliases[key]
        except KeyError:
            raise AttributeError(key)
        if callable(query):
            query = query()
        return self(**query)

    def alias(self, key, **query):
        """
        Create an alias for a query.

        Parameters
        ----------
        key : string
            must be a valid Python identifier
        query :
            keyword argument comprising a query

        Examples
        --------
        >>> db.alias('cal', purpose='calibration')
        """
        if hasattr(self, key) and key not in self.aliases:
            raise ValueError("'%s' is not a legal alias." % key)
        self.aliases[key] = query

    def dynamic_alias(self, key, func):
        """
        Create an alias for a "dynamic" query, a function that returns a query.

        Parameters
        ----------
        key : string
            must be a valid Python identifier
        func : callable
            When called with no arguments, must return a dict that is a valid
            query.

        Examples
        --------
        Get headers from the last 24 hours.
        >>> import time
        >>> db.dynamic_alias('today',
                             lambda: {'start_time': start_time=time.time()- 24*60*60})
        """
        if hasattr(self, key) and key not in self.aliases:
            raise ValueError("'%s' is not a legal alias." % key)
        self.aliases[key] = func

    def __call__(self, text_search=None, **kwargs):
        """Given search criteria, find Headers describing runs.

        This function returns a list of dictionary-like objects encapsulating
        the metadata for a run -- start time, instruments used, and so on.
        In addition to the Parameters below, advanced users can specifiy
        arbitrary queries that are passed through to mongodb.

        Parameters
        ----------
        text_search : str, optional
            search full text of RunStart documents
        start_time : time-like, optional
            Include Headers for runs started after this time. Valid
            "time-like" representations are:
                - float timestamps (seconds since 1970), such as time.time()
                - '2015'
                - '2015-01'
                - '2015-01-30'
                - '2015-03-30 03:00:00'
                - Python datetime objects, such as datetime.datetime.now()
        stop_time: time-like, optional
            Include Headers for runs started before this time. See
            `start_time` above for examples.
        beamline_id : str, optional
            String identifier for a specific beamline
        project : str, optional
            Project name
        owner : str, optional
            The username of the logged-in user when the scan was performed
        scan_id : int, optional
            Integer scan identifier
        uid : str, optional
            Globally unique id string provided to metadatastore
        data_key : str, optional
            The alias (e.g., 'motor1') or PV identifier of data source

        Returns
        -------
        data : list
            Header objects

        Examples
        --------
        >>> DataBroker('keyword')  # full text search
        >>> DataBroker(start_time='2015-03-05', stop_time='2015-03-10')
        >>> DataBroker(data_key='motor1')
        >>> DataBroker(data_key='motor1', start_time='2015-03-05')
        """
        data_key = kwargs.pop('data_key', None)
        if text_search is not None:
            query = {'$and': [{'$text': {'$search': text_search}}]
                               + self.filters}
        else:
            # Include empty {} here so that '$and' gets at least one query.
            self._format_time(kwargs)
            query = {'$and': [{}] + [kwargs] + self.filters}
        run_start = self.mds.find_run_starts(**query)

        headers = []
        for rs in run_start:
            header = Header.from_run_start(self.mds, rs)
            if data_key is None:
                headers.append(header)
                continue
            else:
                # Only include this header in the result if `data_key` is found
                # in one of its descriptors' data_keys.
                for descriptor in header.descriptors:
                    if data_key in descriptor['data_keys']:
                        headers.append(header)
                        break
        return headers

    def find_headers(self, **kwargs):
        "This function is deprecated."
        warnings.warn("Use .__call__() instead of .find_headers()")
        return self(**kwargs)

    def fetch_events(self, headers, fill=True):
        "This function is deprecated."
        warnings.warn("Use .get_events() instead.")
        return self.get_events(headers, fill=fill)

    def fill_event(self, event, handler_registry=None, handler_overrides=None):
        """
        Populate events with externally stored data.

        Parameters
        ----------
        event : document
        handler_registry : dict, optional
            mapping spec names (strings) to handlers (callable classes)
        handler_overrides : dict, optional
            mapping data keys (strings) to handlers (callable classes)
        """
        _fill_event(self.fs, event, handler_registry=handler_registry,
                    handler_overrides=handler_overrides)

    def get_events(self, headers, fields=None, stream_name=ALL, fill=False,
                   handler_registry=None, handler_overrides=None, **kwargs):
        """
        Get Events from given run(s).

        Parameters
        ----------
        headers : Header or iterable of Headers
            The headers to fetch the events for
        fields : list, optional
            whitelist of field names of interest; if None, all are returned
        fill : bool, optional
            Whether externally-stored data should be filled in. Defaults to True
        stream_name : string, optional
            Get events from only one "event stream" with this name. Default
            value is special sentinel class, ``ALL``, which gets all streams
            together.
        handler_registry : dict, optional
            mapping filestore specs (strings) to handlers (callable classes)
        handler_overrides : dict, optional
            mapping data keys (strings) to handlers (callable classes)
        kwargs
            passed through the any plugins

        Yields
        ------
        event : Event
            The event, optionally with non-scalar data filled in

        Raises
        ------
        ValueError if any key in `fields` is not in at least one descriptor pre header.
        """
        res = _get_events(mds=self.mds, fs=self.fs, headers=headers,
                          fields=fields, stream_name=stream_name, fill=fill,
                          handler_registry=handler_registry,
                          handler_overrides=handler_overrides,
                          plugins=self.plugins, **kwargs)
        for event in res:
            yield event

    def get_table(self, headers, fields=None, stream_name='primary',
                  fill=False,
                  convert_times=True, timezone=None, handler_registry=None,
                  handler_overrides=None, localize_times=True):
        """
        Make a table (pandas.DataFrame) from given run(s).

        Parameters
        ----------
        headers : Header or iterable of Headers
            The headers to fetch the events for
        fields : list, optional
            whitelist of field names of interest; if None, all are returned
        stream_name : string, optional
            Get data from a single "event stream." To obtain one comprehensive
            table with all streams, use ``stream_name=ALL`` (where ``ALL`` is a
            sentinel class defined in this module). The default name is
            'primary', but if no event stream with that name is found, the
            default reverts to ``ALL`` (for backward-compatibility).
        fill : bool, optional
            Whether externally-stored data should be filled in.
            Defaults to True
        convert_times : bool, optional
            Whether to convert times from float (seconds since 1970) to
            numpy datetime64, using pandas. True by default.
        timezone : str, optional
            e.g., 'US/Eastern'; if None, use metadatastore configuration in
            `self.mds.config['timezone']`
        handler_registry : dict, optional
            mapping filestore specs (strings) to handlers (callable classes)
        handler_overrides : dict, optional
            mapping data keys (strings) to handlers (callable classes)
        localize_times : bool, optional
            If the times should be localized to the 'local' time zone.  If
            True (the default) the time stamps are converted to the localtime
            zone (as configure in mds).

            This is problematic for several reasons:

              - apparent gaps or duplicate times around DST transitions
              - incompatibility with every other time stamp (which is in UTC)

            however, this makes the dataframe repr look nicer

            This implies convert_times.

            Defaults to True to preserve back-compatibility.

        Returns
        -------
        table : pandas.DataFrame
        """
        if timezone is None:
            timezone = self.mds.config['timezone']
        res = _get_table(mds=self.mds, fs=self.fs, headers=headers,
                         fields=fields, stream_name=stream_name, fill=fill,
                         convert_times=convert_times,
                         timezone=timezone, handler_registry=handler_registry,
                         handler_overrides=handler_overrides,
                         localize_times=localize_times)
        return res

    def get_images(self, headers, name, handler_registry=None,
                   handler_override=None):
        """
        Load images from a detector for given Header(s).

        Parameters
        ----------
        fs: FileStoreRO
        headers : Header or list of Headers
        name : string
            field name (data key) of a detector
        handler_registry : dict, optional
            mapping spec names (strings) to handlers (callable classes)
        handler_override : callable class, optional
            overrides registered handlers


        Example
        -------
        >>> header = DataBroker[-1]
        >>> images = Images(header, 'my_detector_lightfield')
        >>> for image in images:
                # do something
        """
        return Images(self.mds, self.fs, headers, name, handler_registry,
                      handler_override)

    def get_resource_uids(self, header):
        '''Given a Header, give back a list of resource uids

        These uids are required to move the underlying files.

        Parameters
        ----------
        header : Header

        Returns
        -------
        ret : set
            set of resource uids which are refereneced by this
            header.
        '''
        external_keys = set()
        for d in header['descriptors']:
            for k, v in six.iteritems(d['data_keys']):
                if 'external' in v:
                    external_keys.add(k)
        ev_gen = self.get_events(header, stream_name=ALL,
                                 fields=external_keys, fill=False)
        resources = set()
        for ev in ev_gen:
            for k, v in six.iteritems(ev['data']):
                if k in external_keys:
                    res = self.fs.resource_given_eid(v)
                    resources.add(res['uid'])
        return resources

    def restream(self, headers, fields=None, fill=False):
        """
        Get all Documents from given run(s).

        Parameters
        ----------
        headers : Header or iterable of Headers
            header or headers to fetch the documents for
        fields : list, optional
            whitelist of field names of interest; if None, all are returned
        fill : bool, optional
            Whether externally-stored data should be filled in. Defaults to
            False.

        Yields
        ------
        name, doc : tuple
            string name of the Document type and the Document itself.
            Example: ('start', {'time': ..., ...})

        Example
        -------
        >>> def f(name, doc):
        ...     # do something
        ...
        >>> h = DataBroker[-1]  # most recent header
        >>> for name, doc in restream(h):
        ...     f(name, doc)

        Note
        ----
        This output can be used as a drop-in replacement for the output of the
        bluesky Run Engine.

        See Also
        --------
        process
        """
        res = _restream(self.mds, self.fs, headers, fields=fields, fill=fill)
        for name_doc_pair in res:
            yield name_doc_pair

    stream = restream  # compat

    def process(self, headers, func, fields=None, fill=False):
        """
        Get all Documents from given run to a callback.

        Parameters
        ----------
        headers : Header or iterable of Headers
            header or headers to process documents from
        func : callable
            function with the signature `f(name, doc)`
            where `name` is a string and `doc` is a dict
        fields : list, optional
            whitelist of field names of interest; if None, all are returned
        fill : bool, optional
            Whether externally-stored data should be filled in. Defaults to
            False.

        Example
        -------
        >>> def f(name, doc):
        ...     # do something
        ...
        >>> h = DataBroker[-1]  # most recent header
        >>> process(h, f)

        Note
        ----
        This output can be used as a drop-in replacement for the output of the
        bluesky Run Engine.

        See Also
        --------
        restream
        """
        _process(mds=self.mds, fs=self.fs, headers=headers, func=func,
                 fields=fields, fill=fill)

    get_fields = staticmethod(get_fields)  # for convenience

    def export(self, headers, db, new_root=None, copy_kwargs=None):
        """
        Export a list of headers.

        Parameters:
        -----------
        headers : databroker.header
            one or more headers that are going to be exported
        db : databroker.Broker
            an instance of databroker.Broker class, which has
            filestore (fs) and metadatastore (mds) attributes
            that will be the target to export info
        new_root : str
            optional. root directory of files that are going to
            be exported
        copy_kwargs : dict or None
            passed through to the ``copy_files`` method on FileStore;
            None by default

        Returns
        ------
        file_pairs : list
            list of (old_file_path, new_file_path) pairs generated by
            ``copy_files`` moethdo on FileStore.
        """
        if copy_kwargs is None:
            copy_kwargs = {}
        try:
            headers.items()
        except AttributeError:
            pass
        else:
            headers = [headers]
        file_pairs = []
        for header in headers:
            # insert mds
            db.mds.insert_run_start(**header['start'].to_name_dict_pair()[1])
            events = self.get_events(header)
            for descriptor in header['descriptors']:
                db.mds.insert_descriptor(**descriptor.to_name_dict_pair()[1])
                for event in events:
                    event = event.to_name_dict_pair()[1]
                    # 'filled' is obtained from the descriptor, not stored
                    # in each event.
                    event.pop('filled', None)
                    db.mds.insert_event(**event)
            db.mds.insert_run_stop(**header['stop'].to_name_dict_pair()[1])
            # insert fs
            res_uids = self.get_resource_uids(header)
            for uid in res_uids:
                fps = self.fs.copy_files(uid, new_root=new_root, **copy_kwargs)
                file_pairs.extend(fps)
                res = self.fs.resource_given_uid(uid)
                new_res = db.fs.insert_resource(res['spec'],
                                                res['resource_path'],
                                                res['resource_kwargs'],
                                                root=new_root)
                # Note that new_res has a different resource id than res.
                datums = self.fs.datum_gen_given_resource(uid)
                for datum in datums:
                    db.fs.insert_datum(new_res,
                                       datum['datum_id'],
                                       datum['datum_kwargs'])
        return file_pairs


class ArchiverPlugin(object):
    def __init__(self, url, timezone):
        """
        DataBroker plugin

        Parameters
        ----------
        url : string
            e.g., 'http://host:port/'
        timezone : string
            e.g., 'US/Eastern'

        Example
        -------
        >>> p = ArchiverPlugin('http://xf16idc-ca.cs.nsls2.local:17668/',
        ...                    'US/Eastern')
        >>> db = Broker(mds, fs, plugins={'archiver_pvs': p})
        >>> header = db[-1]
        >>> db.get_events(header, archiver_pvs=['...'])
        """
        if not url.endswith('/'):
            url += '/'
        self.url = url
        self.archiver_addr = self.url + "retrieval/data/getData.json"
        self.tz = pytz.timezone(timezone)

    def get_events(self, header, pvs):
        """
        Return results of an EPICS Archiver Appliance query in Event documents.

        That is, mock Event documents so that data from Archiver can be
        analyzed the same as data from metadatastore.

        Parameters
        ----------
        header : Header
        pvs : list or dict
            a list of PVs or a dict mapping PVs to human-friendly names
        """
        if hasattr(pvs, 'items'):
            # Interpret pvs as a dict mapping PVs to names.
            pass
        else:
            # Interpret pvs as a list, and use PVs themselves as names.
            pvs = {pv: pv for pv in pvs}
        start_time, stop_time = header['start']['time'], header['stop']['time']
        for pv, name in pvs.items():
            _from = _munge_time(start_time, self.tz)
            _to = _munge_time(stop_time, self.tz)
            params = {'pv': pv, 'from': _from, 'to': _to}
            req = requests.get(self.archiver_addr, params=params, stream=True)
            req.raise_for_status()
            raw, = req.json()
            timestamps = [x['secs'] for x in raw['data']]
            data = [x['val'] for x in raw['data']]
            # Roll these into an Event document.
            descriptor = {'time': start_time,
                          'uid': 'ephemeral-' + str(uuid.uuid4()),
                          'data_keys': {name: {'source': pv, 'shape': [],
                                               'dtype': 'number'}},
                          # TODO Mark this as 'external' once Broker stops
                          # assuming that all external data in is filestore.
                          'run_start': header['start'],
                          'external_query': params,
                          'external_url': self.url}
            descriptor = Document('EventDescriptor', descriptor)
            for d, t in zip(data, timestamps):
                doc = {'data': {name: d}, 'timestamps': {name: t}, 'time': t,
                       'uid': 'ephemeral-' + str(uuid.uuid4()),
                       'descriptor': descriptor}
                yield Document('Event', doc)


def _munge_time(t, timezone):
    """Close your eyes and trust @arkilic

    Parameters
    ----------
    t : float
        POSIX (seconds since 1970)
    timezone : pytz object
        e.g. ``pytz.timezone('US/Eastern')``

    Return
    ------
    time
        as ISO-8601 format
    """
    t = datetime.fromtimestamp(t)
    return timezone.localize(t).replace(microsecond=0).isoformat()


def store_dec(db, external_writers=None):
    """Decorate a generator of documents to save them to the databases.

    The input stream of (name, document) pairs passes through unchanged.
    As a side effect, documents are inserted into the databases and external
    files may be written.

    Parameters
    ----------
    db: ``databroker.Broker`` instance
        The databroker to store the documents in, must have writeable
        metadatastore and writeable filestore if ``external`` is not empty.
    external_writers : dict
        Maps data keys to a ``WriterClass``, which is responsible for writing
        data to disk and creating a record in filestore. It will be
        instantiated (possible multiple times) with the argument ``db.fs``.
        If it requires additional arguments, use ``functools.partial`` to
        produce a callable that requires only ``db.fs`` to instantiate the
        ``WriterClass``.
        """
    if external_writers is None:
        external_writers = {}  # {'name': WriterClass}

    def wrap(f):
        def wrapped_f(*args, **kwargs):
            gen = f(*args, **kwargs)
            for name, doc in gen:
                # doc will pass through unchanged; fs_doc may be modified to
                # replace some values with references to filestore.
                fs_doc = dict(doc)

                if name == 'start':
                    # Make a fresh instance of any WriterClass classes.
                    writers = {data_key: cl(db.fs)
                               for data_key, cl in external_writers.items()}

                if name == 'descriptor':
                    # Mutate fs_doc here to mark data as external.
                    for data_name in external_writers.keys():
                        # data doesn't have to exist
                        if data_name in fs_doc['data_keys']:
                            fs_doc['data_keys'][data_name].update(
                                external='FILESTORE:')

                elif name == 'event':
                    # We need a selectively deeper copy since we will mutate
                    # fs_doc['data'].
                    fs_doc['data'] = dict(fs_doc['data'])
                    # The writer writes data to an external file, creates a
                    # datum record in the filestore database, and return that
                    # datum_id. We modify fs_doc in place, replacing the data
                    # values with that datum_id.
                    for data_key, writer in writers.items():
                        # data doesn't have to exist
                        if data_key in fs_doc['data']:
                            fs_uid = writer.write(fs_doc['data'][data_key])
                            fs_doc['data'][data_key] = fs_uid

                    doc.update(
                        filled={k: False for k in external_writers.keys()})

                elif name == 'stop':
                    for data_key, writer in list(writers.items()):
                        writer.close()
                        writers.pop(data_key)

                # The mutated fs_doc is inserted into metadatastore.
                fs_doc.pop('filled', None)
                fs_doc.pop('_name', None)
                db.mds.insert(name, fs_doc)

                # The pristine doc is yielded.
                yield name, doc

        return wrapped_f

    return wrap


def event_map(stream_name, data_keys, provenance):
    """
    Map a function onto each event in a stream.

    Parameters
    ----------
    stream_name : string
        e.g., 'primary' or 'baseline'
    data_keys : dict
        key(s) in event['data'] to apply function to (e.g., 'image') mapped to
        a dict with any updates to the data key value
        (e.g., {'shape': [10, 10]}). In the simple case where the shape,
        datatype, etc. are unchanged, the dict is just empty:
        ``{'image': {}}`` and the original metadata passed through.
    proveance : dict
        metadata about this operation
    """
    def outer(f):
        def inner(stream):
            run_start_uid = None
            descriptor_uid = None
            for name, doc in stream:
                if name == 'start':
                    run_start_uid = str(uuid.uuid4())
                    new_start_doc = dict(uid=run_start_uid,
                                         time=time.time(),
                                         parents=[doc['uid']],
                                         provenance=provenance)
                    yield 'start', new_start_doc

                elif name == 'descriptor' and doc.get('name') == stream_name:
                    if run_start_uid is None:
                        raise RuntimeError("Received EventDescriptor before "
                                           "RunStart.")
                    descriptor_uid = doc_or_uid_to_uid(doc)
                    new_data_keys = dict(doc['data_keys'])
                    for k, v in new_data_keys.items():
                        new_data_keys[k].update(v)
                    new_descriptor_uid = str(uuid.uuid4())
                    new_descriptor = dict(uid=new_descriptor_uid,
                                          time=time.time(),
                                          run_start=run_start_uid,
                                          data_keys=new_data_keys,
                                          name=stream_name)
                    yield 'descriptor', new_descriptor

                elif (name == 'event' and
                      doc_or_uid_to_uid(doc['descriptor']) == descriptor_uid):
                    if run_start_uid is None:
                        raise RuntimeError("Received Event before RunStart.")
                    try:
                        new_event = dict(doc)
                        # We need a selectively deeper copy since we will
                        # mutate the contents of new_event['data'].
                        new_event['data'] = dict(new_event['data'])
                        new_event['uid'] = str(uuid.uuid4())
                        new_event['descriptor'] = new_descriptor_uid
                        for data_key in new_data_keys:
                            value = doc['data'][data_key]
                            new_event['data'][data_key] = f(value)
                        yield 'event', new_event
                    except Exception as e:
                        new_stop = dict(uid=str(uuid.uuid4()),
                                        time=time.time(),
                                        run_start=run_start_uid,
                                        exit_status='failure')
                        yield 'stop', new_stop
                        raise

                elif name == 'stop':
                    if run_start_uid is None:
                        raise RuntimeError("Received RunStop before RunStart.")
                    new_stop = dict(uid=str(uuid.uuid4()),
                                    time=time.time(),
                                    run_start=run_start_uid,
                                    exit_status='success')
                    descriptor_uid = None
                    run_start_uid = None
                    yield 'stop', new_stop
        return inner
    return outer


def header_io(db_in, db_out):
    def outer(f):
        def inner(header):
            output_uids = []
            stream = db_in.restream(header, fill=True)
            for name, doc in f(stream):
                if name == 'start':
                    output_uids.append(doc_or_uid_to_uid(doc))
            return db_out[output_uids]
        return inner
    return outer
