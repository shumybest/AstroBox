# coding=utf-8
from octoprint.filemanager.destinations import FileDestinations

__author__ = "Gina Häußge <osd@foosel.net>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'

from flask.ext.principal import identity_changed, Identity
from tornado.web import StaticFileHandler, HTTPError, RequestHandler, asynchronous
from tornado.httpclient import AsyncHTTPClient, HTTPRequest
from flask import url_for, make_response, request, current_app
from flask.ext.login import login_required, login_user, current_user
from werkzeug.utils import redirect
from sockjs.tornado import SockJSConnection

import datetime
import stat
import mimetypes
import email
import time
import os
import threading
import logging
from functools import wraps
from watchdog.events import PatternMatchingEventHandler

from octoprint.settings import settings
import octoprint.timelapse
import octoprint.server
from octoprint.users import ApiUser
from octoprint.events import Events
from octoprint import gcodefiles
from astroprint.boxrouter import boxrouterManager
import octoprint.util as util

def restricted_access(func, apiEnabled=True):
	"""
	If you decorate a view with this, it will ensure that first setup has been
	done for OctoPrint's Access Control plus that any conditions of the
	login_required decorator are met. It also allows to login using the masterkey or any
	of the user's apikeys if API access is enabled globally and for the decorated view.

	If OctoPrint's Access Control has not been setup yet (indicated by the "firstRun"
	flag from the settings being set to True and the userManager not indicating
	that it's user database has been customized from default), the decorator
	will cause a HTTP 403 status code to be returned by the decorated resource.

	If an API key is provided and it matches a known key, the user will be logged in and
	the view will be called directly. If the provided key doesn't match any known key,
	a HTTP 403 status code will be returned by the decorated resource.

	Otherwise the result of calling login_required will be returned.
	"""
	@wraps(func)
	def decorated_view(*args, **kwargs):
		# if OctoPrint hasn't been set up yet, abort
		if settings().getBoolean(["server", "firstRun"]) and (octoprint.server.userManager is None or not octoprint.server.userManager.hasBeenCustomized()):
			return make_response("OctoPrint isn't setup yet", 403)

		# if API is globally enabled, enabled for this request and an api key is provided that is not the current UI API key, try to use that
		apikey = getApiKey(request)
		if settings().get(["api", "enabled"]) and apiEnabled and apikey is not None and apikey != octoprint.server.UI_API_KEY:
			if apikey == settings().get(["api", "key"]):
				# master key was used
				user = ApiUser()
			else:
				# user key might have been used
				user = octoprint.server.userManager.findUser(apikey=apikey)

			if user is None:
				return make_response("Invalid API key", 401)
			if login_user(user, remember=False):
				identity_changed.send(current_app._get_current_object(), identity=Identity(user.get_id()))
				return func(*args, **kwargs)

		# call regular login_required decorator
		#TODO: remove this temporary disablement of login requirement
		#return login_required(func)(*args, **kwargs)
		return func(*args, **kwargs)
	return decorated_view


def api_access(func):
	@wraps(func)
	def decorated_view(*args, **kwargs):
		if not settings().get(["api", "enabled"]):
			make_response("API disabled", 401)
		apikey = getApiKey(request)
		if apikey is None:
			make_response("No API key provided", 401)
		if apikey != settings().get(["api", "key"]):
			make_response("Invalid API key", 403)
		return func(*args, **kwargs)
	return decorated_view


def getUserForApiKey(apikey):
	if settings().get(["api", "enabled"]) and apikey is not None:
		if apikey == settings().get(["api", "key"]):
			# master key was used
			return ApiUser()
		else:
			# user key might have been used
			return octoprint.server.userManager.findUser(apikey=apikey)
	else:
		return None


def getApiKey(request):
	# Check Flask GET/POST arguments
	if hasattr(request, "values") and "apikey" in request.values:
		return request.values["apikey"]

	# Check Tornado GET/POST arguments
	if hasattr(request, "arguments") and "apikey" in request.arguments \
		and len(request.arguments["apikey"]) > 0 and len(request.arguments["apikey"].strip()) > 0:
		return request.arguments["apikey"]

	# Check Tornado and Flask headers
	if "X-Api-Key" in request.headers.keys():
		return request.headers.get("X-Api-Key")

	return None


#~~ Printer state


class PrinterStateConnection(SockJSConnection):
	EVENTS = [Events.UPDATED_FILES, Events.METADATA_ANALYSIS_FINISHED, Events.MOVIE_RENDERING, Events.MOVIE_DONE,
			  Events.MOVIE_FAILED, Events.SLICING_STARTED, Events.SLICING_DONE, Events.SLICING_FAILED,
			  Events.TRANSFER_STARTED, Events.TRANSFER_DONE, Events.CLOUD_DOWNLOAD, Events.ASTROPRINT_STATUS, Events.SOFTWARE_UPDATE]

	def __init__(self, printer, gcodeManager, userManager, eventManager, session):
		SockJSConnection.__init__(self, session)

		self._logger = logging.getLogger(__name__)

		self._temperatureBacklog = []
		self._temperatureBacklogMutex = threading.Lock()
		self._logBacklog = []
		self._logBacklogMutex = threading.Lock()
		self._messageBacklog = []
		self._messageBacklogMutex = threading.Lock()

		self._printer = printer
		self._gcodeManager = gcodeManager
		self._userManager = userManager
		self._eventManager = eventManager

	def _getRemoteAddress(self, info):
		forwardedFor = info.headers.get("X-Forwarded-For")
		if forwardedFor is not None:
			return forwardedFor.split(",")[0]
		return info.ip

	def on_open(self, info):
		remoteAddress = self._getRemoteAddress(info)
		self._logger.info("New connection from client: %s" % remoteAddress)

		# connected => update the API key, might be necessary if the client was left open while the server restarted
		self._emit("connected", {"apikey": octoprint.server.UI_API_KEY, "version": octoprint.server.VERSION})
		self.sendEvent(Events.ASTROPRINT_STATUS, boxrouterManager().status)

		self._printer.registerCallback(self)
		self._gcodeManager.registerCallback(self)
		octoprint.timelapse.registerCallback(self)

		self._eventManager.fire(Events.CLIENT_OPENED, {"remoteAddress": remoteAddress})
		for event in PrinterStateConnection.EVENTS:
			self._eventManager.subscribe(event, self._onEvent)

		octoprint.timelapse.notifyCallbacks(octoprint.timelapse.current)

	def on_close(self):
		self._logger.info("Client connection closed")
		self._printer.unregisterCallback(self)
		self._gcodeManager.unregisterCallback(self)
		octoprint.timelapse.unregisterCallback(self)

		self._eventManager.fire(Events.CLIENT_CLOSED)
		for event in PrinterStateConnection.EVENTS:
			self._eventManager.unsubscribe(event, self._onEvent)

	def on_message(self, message):
		pass

	def sendCurrentData(self, data):
		# add current temperature, log and message backlogs to sent data
		with self._temperatureBacklogMutex:
			temperatures = self._temperatureBacklog
			self._temperatureBacklog = []

		with self._logBacklogMutex:
			logs = self._logBacklog
			self._logBacklog = []

		with self._messageBacklogMutex:
			messages = self._messageBacklog
			self._messageBacklog = []

		data.update({
			"temps": temperatures,
			"logs": logs,
			"messages": messages
		})
		self._emit("current", data)

	def sendHistoryData(self, data):
		self._emit("history", data)

	def sendEvent(self, type, payload=None):
		self._emit("event", {"type": type, "payload": payload})

	def sendFeedbackCommandOutput(self, name, output):
		self._emit("feedbackCommandOutput", {"name": name, "output": output})

	def sendTimelapseConfig(self, timelapseConfig):
		self._emit("timelapse", timelapseConfig)

	def addLog(self, data):
		with self._logBacklogMutex:
			self._logBacklog.append(data)

	def addMessage(self, data):
		with self._messageBacklogMutex:
			self._messageBacklog.append(data)

	def addTemperature(self, data):
		with self._temperatureBacklogMutex:
			self._temperatureBacklog.append(data)

	def _onEvent(self, event, payload):
		self.sendEvent(event, payload)

	def _emit(self, type, payload):
		self.send({type: payload})


#~~ customized large response handler


class LargeResponseHandler(StaticFileHandler):

	CHUNK_SIZE = 16 * 1024

	def initialize(self, path, default_filename=None, as_attachment=False, access_validation=None):
		StaticFileHandler.initialize(self, path, default_filename)
		self._as_attachment = as_attachment
		self._access_validation = access_validation

	def get(self, path, include_body=True):
		if self._access_validation is not None:
			self._access_validation(self.request)

		path = self.parse_url_path(path)
		abspath = os.path.abspath(os.path.join(self.root, path))
		# os.path.abspath strips a trailing /
		# it needs to be temporarily added back for requests to root/
		if not (abspath + os.path.sep).startswith(self.root):
			raise HTTPError(403, "%s is not in root static directory", path)
		if os.path.isdir(abspath) and self.default_filename is not None:
			# need to look at the request.path here for when path is empty
			# but there is some prefix to the path that was already
			# trimmed by the routing
			if not self.request.path.endswith("/"):
				self.redirect(self.request.path + "/")
				return
			abspath = os.path.join(abspath, self.default_filename)
		if not os.path.exists(abspath):
			raise HTTPError(404)
		if not os.path.isfile(abspath):
			raise HTTPError(403, "%s is not a file", path)

		stat_result = os.stat(abspath)
		modified = datetime.datetime.fromtimestamp(stat_result[stat.ST_MTIME])

		self.set_header("Last-Modified", modified)

		mime_type, encoding = mimetypes.guess_type(abspath)
		if mime_type:
			self.set_header("Content-Type", mime_type)

		cache_time = self.get_cache_time(path, modified, mime_type)

		if cache_time > 0:
			self.set_header("Expires", datetime.datetime.utcnow() +
									   datetime.timedelta(seconds=cache_time))
			self.set_header("Cache-Control", "max-age=" + str(cache_time))

		self.set_extra_headers(path)

		# Check the If-Modified-Since, and don't send the result if the
		# content has not been modified
		ims_value = self.request.headers.get("If-Modified-Since")
		if ims_value is not None:
			date_tuple = email.utils.parsedate(ims_value)
			if_since = datetime.datetime.fromtimestamp(time.mktime(date_tuple))
			if if_since >= modified:
				self.set_status(304)
				return

		if not include_body:
			assert self.request.method == "HEAD"
			self.set_header("Content-Length", stat_result[stat.ST_SIZE])
		else:
			with open(abspath, "rb") as file:
				while True:
					data = file.read(LargeResponseHandler.CHUNK_SIZE)
					if not data:
						break
					self.write(data)
					self.flush()

	def set_extra_headers(self, path):
		if self._as_attachment:
			self.set_header("Content-Disposition", "attachment")


##~~ URL Forward Handler for forwarding requests to a preconfigured static URL


class UrlForwardHandler(RequestHandler):

	def initialize(self, url=None, as_attachment=False, basename=None, access_validation=None):
		RequestHandler.initialize(self)
		self._url = url
		self._as_attachment = as_attachment
		self._basename = basename
		self._access_validation = access_validation

	@asynchronous
	def get(self, *args, **kwargs):
		if self._access_validation is not None:
			self._access_validation(self.request)

		if self._url is None:
			raise HTTPError(404)

		client = AsyncHTTPClient()
		r = HTTPRequest(url=self._url, method=self.request.method, body=self.request.body, headers=self.request.headers, follow_redirects=False, allow_nonstandard_methods=True)

		try:
			return client.fetch(r, self.handle_response)
		except HTTPError as e:
			if hasattr(e, "response") and e.response:
				self.handle_response(e.response)
			else:
				raise HTTPError(500)

	def handle_response(self, response):
		if response.error and not isinstance(response.error, HTTPError):
			raise HTTPError(500)

		filename = None

		self.set_status(response.code)
		for name in ("Date", "Cache-Control", "Server", "Content-Type", "Location"):
			value = response.headers.get(name)
			if value:
				self.set_header(name, value)

				if name == "Content-Type":
					filename = self.get_filename(value)

		if self._as_attachment:
			if filename is not None:
				self.set_header("Content-Disposition", "attachment; filename=%s" % filename)
			else:
				self.set_header("Content-Disposition", "attachment")

		if response.body:
			self.write(response.body)
		self.finish()

	def get_filename(self, content_type):
		if not self._basename:
			return None

		typeValue = map(str.strip, content_type.split(";"))
		if len(typeValue) == 0:
			return None

		extension = mimetypes.guess_extension(typeValue[0])
		if not extension:
			return None

		return "%s%s" % (self._basename, extension)


#~~ admin access validator for use with tornado


def admin_validator(request):
	"""
	Validates that the given request is made by an admin user, identified either by API key or existing Flask
	session.

	Must be executed in an existing Flask request context!

	:param request: The Flask request object
	"""

	apikey = getApiKey(request)
	if settings().get(["api", "enabled"]) and apikey is not None:
		user = getUserForApiKey(apikey)
	else:
		user = current_user

	if user is None or not user.is_authenticated() or not user.is_admin():
		raise HTTPError(403)


#~~ user access validator for use with tornado


def user_validator(request):
	"""
	Validates that the given request is made by an authenticated user, identified either by API key or existing Flask
	session.

	Must be executed in an existing Flask request context!

	:param request: The Flask request object
	"""

	apikey = getApiKey(request)
	if settings().get(["api", "enabled"]) and apikey is not None:
		user = getUserForApiKey(apikey)
	else:
		user = current_user

	if user is None or not user.is_authenticated():
		raise HTTPError(403)


#~~ reverse proxy compatible wsgi middleware


class ReverseProxied(object):
	"""
	Wrap the application in this middleware and configure the
	front-end server to add these headers, to let you quietly bind
	this to a URL other than / and to an HTTP scheme that is
	different than what is used locally.

	In nginx:
		location /myprefix {
			proxy_pass http://192.168.0.1:5001;
			proxy_set_header Host $host;
			proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
			proxy_set_header X-Scheme $scheme;
			proxy_set_header X-Script-Name /myprefix;
		}

	Alternatively define prefix and scheme via config.yaml:
		server:
			baseUrl: /myprefix
			scheme: http

	:param app: the WSGI application
	"""

	def __init__(self, app):
		self.app = app

	def __call__(self, environ, start_response):
		script_name = environ.get('HTTP_X_SCRIPT_NAME', '')
		if not script_name:
			script_name = settings().get(["server", "baseUrl"])

		if script_name:
			environ['SCRIPT_NAME'] = script_name
			path_info = environ['PATH_INFO']
			if path_info.startswith(script_name):
				environ['PATH_INFO'] = path_info[len(script_name):]

		scheme = environ.get('HTTP_X_SCHEME', '')
		if not scheme:
			scheme = settings().get(["server", "scheme"])

		if scheme:
			environ['wsgi.url_scheme'] = scheme
		return self.app(environ, start_response)


def redirectToTornado(request, target):
	requestUrl = request.url
	appBaseUrl = requestUrl[:requestUrl.find(url_for("index") + "api")]

	redirectUrl = appBaseUrl + target
	if "?" in requestUrl:
		fragment = requestUrl[requestUrl.rfind("?"):]
		redirectUrl += fragment
	return redirect(redirectUrl)


class UploadCleanupWatchdogHandler(PatternMatchingEventHandler):
	"""
	Takes care of automatically deleting metadata entries for files that get deleted from the uploads folder
	"""

	patterns = map(lambda x: "*.%s" % x, gcodefiles.GCODE_EXTENSIONS)

	def __init__(self, gcode_manager):
		PatternMatchingEventHandler.__init__(self)
		self._gcode_manager = gcode_manager

	def on_deleted(self, event):
		filename = self._gcode_manager._getBasicFilename(event.src_path)
		if not filename:
			return

		self._gcode_manager.removeFileFromMetadata(filename)


class GcodeWatchdogHandler(PatternMatchingEventHandler):
	"""
	Takes care of automatically "uploading" files that get added to the watched folder.
	"""

	patterns = map(lambda x: "*.%s" % x, gcodefiles.SUPPORTED_EXTENSIONS)

	def __init__(self, gcodeManager, printer):
		PatternMatchingEventHandler.__init__(self)

		self._logger = logging.getLogger(__name__)

		self._gcodeManager = gcodeManager
		self._printer = printer

	def _upload(self, path):
		class WatchdogFileWrapper(object):

			def __init__(self, path):
				self._path = path
				self.filename = os.path.basename(self._path)

			def save(self, target):
				util.safeRename(self._path, target)

		fileWrapper = WatchdogFileWrapper(path)

		# determine current job
		currentFilename = None
		currentOrigin = None
		currentJob = self._printer.getCurrentJob()
		if currentJob is not None and "file" in currentJob.keys():
			currentJobFile = currentJob["file"]
			if "name" in currentJobFile.keys() and "origin" in currentJobFile.keys():
				currentFilename = currentJobFile["name"]
				currentOrigin = currentJobFile["origin"]

		# determine future filename of file to be uploaded, abort if it can't be uploaded
		futureFilename = self._gcodeManager.getFutureFilename(fileWrapper)
		if futureFilename is None or (not settings().getBoolean(["cura", "enabled"]) and not gcodefiles.isGcodeFileName(futureFilename)):
			self._logger.warn("Could not add %s: Invalid file" % fileWrapper.filename)
			return

		# prohibit overwriting currently selected file while it's being printed
		if futureFilename == currentFilename and not currentOrigin == FileDestinations.SDCARD and self._printer.isPrinting() or self._printer.isPaused():
			self._logger.warn("Could not add %s: Trying to overwrite file that is currently being printed" % fileWrapper.filename)
			return

		self._gcodeManager.addFile(fileWrapper, FileDestinations.LOCAL)

	def on_created(self, event):
		self._upload(event.src_path)


