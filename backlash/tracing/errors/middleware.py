from backlash._compat import string_types, bytes_
from backlash.tbtools import get_current_traceback
from backlash.utils import RequestContext
from pymongo.errors import AutoReconnect

import logging
log = logging.getLogger()


class TraceErrorsMiddleware(object):
    def __init__(self, application, reporters, context_injectors):
        self.app = application
        self.reporters = reporters
        self.context_injectors = context_injectors

    def _report_errors(self, environ, recorded_exc_info=None):
        context = RequestContext({'environ': dict(environ)})
        for injector in self.context_injectors:
            context.update(injector(environ))

        traceback = get_current_traceback(skip=2, show_hidden_frames=False, context=context,
                                          exc_info=recorded_exc_info)
        traceback.log(environ['wsgi.errors'])

        for r in self.reporters:
            try:
                r.report(traceback)
            except Exception:
                error = get_current_traceback(skip=1, show_hidden_frames=False)
                environ['wsgi.errors'].write('\nError while reporting exception with %s\n' % r)
                environ['wsgi.errors'].write(error.plaintext)

    def _report_errors_with_response(self, environ, start_response):
        self._report_errors(environ, None)

        try:
            start_response('500 INTERNAL SERVER ERROR', [
                ('Content-Type', 'text/html; charset=utf-8'),
                # Disable Chrome's XSS protection, the debug
                # output can cause false-positives.
                ('X-XSS-Protection', '0'),
                ])
        except Exception:
            # if we end up here there has been output but an error
            # occurred.  in that situation we can do nothing fancy any
            # more, better log something into the error log and fall
            # back gracefully.
            environ['wsgi.errors'].write(
                'Debugging middleware caught exception in streamed '
                'response at a point where response headers were already '
                'sent.\n')
        else:
            yield bytes_('Internal Server Error')

    def _report_errors_while_consuming_iter(self, app_iter, environ, start_response):
        try:
            for item in app_iter:
                yield item
            if hasattr(app_iter, 'close'):
                app_iter.close()
        except Exception:
            if hasattr(app_iter, 'close'):
                app_iter.close()

            for chunk in self._report_errors_with_response(environ, start_response):
                yield chunk
                
    def setup_ming(self):
        """Setup MongoDB database engine using Ming"""
        from tg import config
        from tg.configuration.utils import coerce_config
        from tg.support.converters import asbool, asint

        try:
            from ming import create_datastore
            def create_ming_datastore(url, database, **kw):
                if database and url[-1] != '/':
                    url += '/'
                ming_url = url + database
                return create_datastore(ming_url, **kw)
        except ImportError: #pragma: no cover
            from ming.datastore import DataStore
            def create_ming_datastore(url, database, **kw):
                return DataStore(url, database, **kw)

        def mongo_read_pref(value):
            from pymongo.read_preferences import ReadPreference
            return getattr(ReadPreference, value)

        datastore_options = coerce_config(config, 'ming.connection.', {'max_pool_size':asint,
                                                                       'network_timeout':asint,
                                                                       'tz_aware':asbool,
                                                                       'safe':asbool,
                                                                       'journal':asbool,
                                                                       'wtimeout':asint,
                                                                       'fsync':asbool,
                                                                       'ssl':asbool,
                                                                       'read_preference':mongo_read_pref})
        datastore_options.pop('host', None)
        datastore_options.pop('port', None)

        datastore = create_ming_datastore(config['ming.url'], config.get('ming.db', ''), **datastore_options)
        config['tg.app_globals'].ming_datastore = datastore

    def __call__(self, environ, start_response):
        app_iter = None
        try:
            app_iter = self.app(environ, start_response)
        except AutoReconnect:
            try:
                self.setup_ming()
                app_iter = self.app(environ, start_response)
            except Exception:
                return list(self._report_errors_with_response(environ, start_response))
        except Exception:
            return list(self._report_errors_with_response(environ, start_response))
        else:
            recorded_exc_info = environ.pop('backlash.exc_info', None)
            if recorded_exc_info is not None:
                environ = environ.pop('backlash.exc_environ', environ)
                self._report_errors(environ, recorded_exc_info)

        return self._report_errors_while_consuming_iter(app_iter, environ, start_response)
