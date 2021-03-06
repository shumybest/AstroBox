# coding=utf-8
__author__ = "Daniel Arroyo <daniel@3dagogo.com>"
__license__ = 'GNU Affero General Public License http://www.gnu.org/licenses/agpl.html'

import socket, urllib2, subprocess

from octoprint.settings import settings

from sys import platform

# singleton
_instance = None

def networkManager():
	global _instance
	if _instance is None:
		if platform == "linux" or platform == "linux2":
			from astroprint.network.debian import DebianNetworkManager
			_instance = DebianNetworkManager()
		elif platform == "darwin":
			from astroprint.network.mac_dev import MacDevNetworkManager
			_instance = MacDevNetworkManager()
		else:
			_instance = NetworkManager()

	return _instance

class NetworkManager(object):
	def __init__(self):
		self.settings = settings()

	def isAstroprintReachable(self):
		try:
			urllib2.urlopen("%s/check" % settings().get(['cloudSlicer','apiHost']),timeout=1)
			return True

		except urllib2.URLError as err: 
			return False

	def checkOnline(self):
		timeout= 1
		addresses= ['8.8.8.8', '8.8.4.4', '208.67.222.222', '208.67.220.220'] #Google DNS(2), OpenDNS(2)

		for addr in addresses:
			status = subprocess.call("ping -W %d -c 1 %s > /dev/null 2>&1" % (timeout, addr), shell=True)
			if status == 0:
				return True
			else:
				continue

		return False

	def conectionStatus(self):
		return 'connected'

	def getWifiNetworks(self):
		return None

	def getWifiDevice(self):
		return None

	def hasWifiDevice(self):
		return None

	def isHotspotAble(self):
		return None

	def getActiveConnections(self):
		return None

	def setWifiNetwork(self, bssid, password):
		return None

	def isHotspotActive(self):
		return None

	def startHotspot(self):
		#return True when succesful
		return "Starting a hotspot is not supported"

	def stopHotspot(self):
		#return True when succesful
		return "Stopping a hotspot is not supported"

	def getHostname(self):
		return None

	def setHostname(self, name):
		return None
