import threading
import stb
import os
import json
import subprocess
import uuid
import logging
import xml.etree.cElementTree as ET
import flask
from flask import (
    Flask,
    render_template,
    redirect,
    request,
    Response,
    make_response,
    flash,
    stream_with_context,
    jsonify, url_for)
import math
import time
import requests
from datetime import datetime, timezone
from dateutil.parser import parse
from functools import wraps
import secrets
import waitress
from collections import defaultdict
import copy

# Lock for multi-threading  
lock = threading.Lock()

app = Flask(__name__)
app.secret_key = secrets.token_urlsafe(32)

logger = logging.getLogger("STB-Proxy")
logFormat = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")
fileHandler = logging.FileHandler("STB-Proxy.log")
fileHandler.setFormatter(logFormat)
logger.addHandler(fileHandler)
consoleFormat = logging.Formatter("[%(levelname)s] %(message)s")
consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(consoleFormat)
logger.addHandler(consoleHandler)

basePath = os.path.abspath(os.getcwd())

if os.getenv("HOST"):
    host = os.getenv("HOST")
else:
    host = "localhost:8001"

if os.getenv("CONFIG"):
    configFile = os.getenv("CONFIG")
else:
    configFile = os.path.join(basePath, "config.json")
    
if os.getenv("DEBUG_MODE"):
    debug_str = os.getenv("DEBUG_MODE")
    debugMode = debug_str.lower() == 'true' or debug_str == '1'
else:
    debugMode = False

# Set log level
if debugMode:
    logger.setLevel(logging.DEBUG)
else:
    logger.setLevel(logging.INFO)

occupied = {}
config = {}

d_ffmpegcmd = "ffmpeg -re -http_proxy <proxy> -timeout <timeout> -i <url> -map 0 -codec copy -f mpegts pipe:"

defaultSettings = {
    "stream method": "ffmpeg",
    "ffmpeg command": "ffmpeg -re -http_proxy <proxy> -timeout <timeout> -i <url> -map 0 -codec copy -f mpegts pipe:",
    "stream timeout": "5",
    "test streams": "true",
    "try all macs": "false",
    "use channel genres": "true",
    "use channel numbers": "true",
    "sort playlist by channel genre": "false",
    "sort playlist by channel number": "false",
    "sort playlist by channel name": "false",
    "enable security": "false",
    "username": "admin",
    "password": "12345",
    "enable hdhr": "false",
    "hdhr name": "STB-Proxy",
    "hdhr id": str(uuid.uuid4().hex),
    "hdhr tuners": "1",
}

# Definition for default mac entry
default_mac_info = {"expiry": None, "stats": {"playtime": 0, "errors": 0, "requests": 0}}

defaultPortal = {
    "enabled": "true",
    "name": "",
    "url": "",
    "macs": defaultdict(lambda: default_mac_info),
    "streams per mac": "1",
    "epgTimeOffset": "0",
    "proxy": "",
    "enabled channels": [],
    "custom channel names": {},
    "custom channel numbers": {},
    "custom genres": {},
    "custom epg ids": {},
    "fallback channels": {},
    "time_zone": "Europe/London",
}

bufferSize = 1024  # Buffer size in bytes (1024=1kB)

def loadConfig():
    
    def check_and_convert_macs(data):
        macs_data = defaultdict(lambda: copy.deepcopy(default_mac_info))
        
        def update_values(default_dict, data_dict):
            for key, value in default_dict.items():
                if isinstance(value, dict):
                    if key in data_dict and isinstance(data_dict[key], dict):
                        update_values(default_dict[key], data_dict[key])
                else:
                    if key in data_dict:
                        default_dict[key] = data_dict[key]
        
        for mac, mac_data in data.items():
            if not isinstance(mac_data, dict):
                newdict = copy.deepcopy(default_mac_info)
                if isinstance(mac_data, str):
                    timestamp = parseExpieryStr(mac_data)
                    newdict["expiry"] = timestamp
                elif isinstance(mac_data, (int, float)):
                    newdict["expiry"] = mac_data
                else:
                    logger.error("Unable to get expiry date for MAC ({}) from old config data.".format(mac))
                macs_data[mac] = newdict
            else:
                update_values(macs_data[mac], mac_data)
        
        return macs_data

    try:
        with open(configFile) as f:
            data = json.load(f)
    except FileNotFoundError:
        logger.warning("No existing config found. Creating a new one")
        data = {}

    data.setdefault("portals", {})
    data.setdefault("settings", {})

    settings = data["settings"]
    settingsOut = {}

    for setting, defaultData in defaultSettings.items():
        value = settings.get(setting)
        if not value or type(defaultData) != type(value):
            value = copy.copy(defaultData)
        settingsOut[setting] = value

    data["settings"] = settingsOut

    portals = data["portals"]
    portalsOut = {}

    for portal, loadedData in portals.items():
        mergedPortalData = {}
        for setting, defaultData in defaultPortal.items():
            value = loadedData.get(setting)
            if setting == "macs":
                value = check_and_convert_macs(value)
            if not value or type(defaultData) != type(value):
                value = copy.copy(defaultData)
            mergedPortalData[setting] = value
        portalsOut[portal] = mergedPortalData

    data["portals"] = portalsOut

    return data

def parseExpieryStr(date_string):
    try:
        # We need dateutil, because the date format is not unique all portals
        # date_obj = datetime.strptime(date_string, "%B %d, %Y, %I:%M %p")
        date_obj = parse(date_string)
        # Convert the datetime object to a Unix timestamp
        timestamp = date_obj.timestamp()
        return timestamp
    except ValueError:
        logger.info("Unable to parse expiration date ({})".format(date_string))
        return None

def checkExpiration(expieryStr):
    def timeLeft(timestamp):
        try:
            # Calculate the time difference in seconds
            current_time = datetime.now().timestamp()
            difference = current_time - timestamp
            return difference
        except Exception as e:
            return None
    
    expTimestamp = parseExpieryStr(expieryStr)
    timeLeft = timeLeft(expTimestamp)
    
    if timeLeft > 0:
        daysLeft = math.floor(timeLeft / (60 * 60 * 24))
        return False, daysLeft
    else:
        return True, 0
        
def getPortals():
    return config["portals"]


def savePortals(portals):
    with open(configFile, "w") as f:
        config["portals"] = portals
        json.dump(config, f, indent=4)


def getSettings():
    return config["settings"]


def saveSettings(settings):
    with open(configFile, "w") as f:
        config["settings"] = settings
        json.dump(config, f, indent=4)
        

def authorise(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        settings = getSettings()
        security = settings["enable security"]
        username = settings["username"]
        password = settings["password"]
        if (
            security == "false"
            or auth
            and auth.username == username
            and auth.password == password
        ):
            return f(*args, **kwargs)

        return make_response(
            "Could not verify your login!",
            401,
            {"WWW-Authenticate": 'Basic realm="Login Required"'},
        )

    return decorated


def moveMac(portalId, mac):
    portals = getPortals()
    logger.info("Moving MAC({}) for Portal({})".format(mac, portals[portalId]["name"]))
    macs = portals[portalId]["macs"]
    x = macs[mac]
    del macs[mac]
    macs[mac] = x
    portals[portalId]["macs"] = macs
    savePortals(portals)



def test_mac_addresses(url, proxy, macs, name, time_zone):
    """
    Tests a list of MAC addresses; tries all proxies (fb1..fb7 per proxy).
    Returns (valid_macs, dead_macs).
    """
    dead_macs = []
    valid_macs = []
    url = stb.getUrl(url)

    for mac in macs:
        try:
            token, used_proxy = stb.getToken_fb_multi(url, mac, proxy, time_zone)
            if not token:
                dead_macs.append({"mac": mac, "reason": "no token across proxies"})
                continue
            pxy = used_proxy or proxy
            stb.getProfile(url, mac, token, pxy, time_zone)
            expiry = stb.getExpires(url, mac, token, pxy, time_zone)
            if not expiry:
                dead_macs.append({"mac": mac, "reason": "no expiry"})
                continue
            valid_macs.append({"mac": mac, "expiry": parseExpieryStr(expiry)})
        except Exception as e:
            dead_macs.append({"mac": mac, "reason": f"error: {type(e).__name__}"})
    return valid_macs, dead_macs


def portal_update_macs(portal, macs=None, retest=False):
    # Retrieve old MAC addresses from portal
    old_macs_dict = portal["macs"]
    
    old_macs_set = set(old_macs_dict.keys() if old_macs_dict else [])
    new_macs_set = set(macs if macs else [])
    common_macs = list(new_macs_set & old_macs_set)     # Intersection of new_macs and old_macs
    unique_new_macs = list(new_macs_set - old_macs_set) # Difference: new_macs - old_macs

    # Determine MACs to test based on retest flag and new_macs input
    if retest:
        # If retest is True, test both old and any new MACs if provided
        macs_to_test = common_macs + unique_new_macs
        common_macs = []
    else:
        # Only test new MACs if retest is False
        macs_to_test = unique_new_macs
        
    if not macs_to_test:
        # No MACs to test, exit function
        logger.info(f"No new MAC addresses in Portal({portal['name']}) found")
        flash(f"No new MAC addresses in Portal({portal['name']}) found", "warning")

    # Test MAC addresses
    valid_macs, dead_macs = test_mac_addresses(portal["url"], portal["proxy"], macs_to_test, portal["name"], portal["time_zone"])
    if old_macs_dict:
        for mac, data in old_macs_dict.items():
            if mac in common_macs and mac not in valid_macs:
                valid_macs.append({'mac': mac, 'expiry': data['expiry']})
            if mac in dead_macs:
                logger.info(f"Dead MAC({mac}) for Portal({portal['name']}) has been removed.")
                flash(f"Dead MAC({mac}) for Portal({portal['name']}) has been removed.", "success")
            
    # Initialize mac info structure and process results
    macsout = defaultdict(lambda: copy.deepcopy(default_mac_info))

    for entry in valid_macs:
        mac = entry["mac"]
        expiry = entry["expiry"] 

        if mac in old_macs_dict:
            # Keep stats for existing MACs and update expiry date
            macsout[mac] = old_macs_dict[mac]
            macsout[mac]["expiry"] = expiry
            if mac in unique_new_macs:
                logger.info(f"Successfully updated MAC({mac}) for Portal({portal['name']})")
                flash(f"Successfully updated MAC({mac}) for Portal({portal['name']})", "success")
        else:
            # Add new MAC address with blank stats
            macsout[mac]["expiry"] = expiry
            logger.info(f"Successfully added MAC({mac}) to Portal({portal['name']})")
            flash(f"Successfully added MAC({mac}) to Portal({portal['name']})", "success")

    # Update the portal's MAC list
    portal["macs"] = macsout

    # Return the updated portal object
    return portal


def getFreeMac(portalId):
    
    # Portal data
    portals = getPortals()
    portal = portals.get(portalId)
    
    macs = list(portal["macs"].keys())
    
    for mac in macs:
        if isMacFree(portalId, mac):
            return mac


def isMacFree(portalId, mac):
    maxWaitTimeFree = 5  # Time to wait for freed mac
    
    # Portal data
    portals = getPortals()
    portal = portals.get(portalId)
    
    streamsPerMac = int(portal.get("streams per mac"))
    
    # When changing channels, it takes a while until the stream is finished and the Mac address gets released    
    checkInterval = 0.1
    maxIterations = max(math.ceil(maxWaitTimeFree / checkInterval), 1)
    for _ in range(maxIterations):
        count = 0
        for i in occupied.get(portalId, []):
            if i["mac"] == mac:
                count = count + 1
        if count < streamsPerMac:
            return True
        else:
            time.sleep(0.1)
    return False


def getChannel(portalId, channelId):
    # Portal data
    portals = getPortals()
    portal = portals.get(portalId)
    
    portalName = portal.get("name")
    macs = list(portal["macs"].keys())

    freeMac = False
    channelName = None
    link = None
    for mac in macs:
        freeMac = isMacFree(portalId, mac)
        if freeMac:
            channelName, link = getChannelByMac(portalId, channelId, mac)
            if link:
                return channelName, link, mac
        
        # Try with next mac address
        moveMac(portalId, mac)

        if not getSettings().get("try all macs", "false") == "true":
            break
        
    if freeMac:
        logger.info(
            "No working streams found for Portal({}):Channel({})".format(
                portalId, channelId
            )
        )
    else:
        logger.info(
            "No free MAC for Portal({}):Channel({})".format(portalId, channelId)
        )

    return channelName, link, mac


def getChannelByMac(portalId, channelId, mac):
    channelName= None
    link = None
    
    # Portal data
    portals = getPortals()
    portal = portals.get(portalId)
    
    portalName = portal.get("name")
    url = portal.get("url")
    streamsPerMac = int(portal.get("streams per mac"))
    proxy = portal.get("proxy")
    time_zone = portal.get("time_zone")
    freeMac = False
    channels = None
    cmd = None
    if streamsPerMac == 0 or isMacFree(portalId, mac):
        logger.info(
            "Trying to get Link for Portal({}):MAC({}):Channel({})".format(portalId, mac, channelId)
        )
        freeMac = True
        token = stb.getToken_fb(url, mac, proxy, time_zone)
        if token:
            stb.getProfile(url, mac, token, proxy, time_zone)
            channels = stb.getAllChannels(url, mac, token, proxy, time_zone)
    else:
        logger.info(
            "Maximum streams for MAC({}) in use.".format(mac)
        )
    if channels:
        for c in channels:
            if str(c["id"]) == channelId:
                channelName = portal.get("custom channel names", {}).get(channelId)
                if channelName == None:
                    channelName = c["name"]
                cmd = c["cmd"]
                break

    if cmd:
        if "http://localhost/" in cmd:
            link = stb.getLink(url, mac, token, cmd, proxy, time_zone)
        else:
            link = cmd.split(" ")[1]
            
    if link:
        logger.info(
            "Link for Channel ""{}"" on Portal({}) using MAC({}) found: {}".format(channelName, portalId, mac, link)
        )
        
    else:
        if freeMac:
            logger.info(
                "Unable to get Link for Channel-ID({}) on Portal({}) using MAC({})".format(channelId, portalId, mac)
            )
        else:
            logger.info(
                "MAC ({}) is not free for Portal({})".format(mac, portalId)
            )

    return channelName, link


def testStream(link, proxy=None):
    timeout = int(getSettings()["stream timeout"]) * int(1000000)
    ffprobecmd = ["ffprobe", "-timeout", str(timeout), "-i", link]

    if proxy:
        ffprobecmd.insert(1, "-http_proxy")
        ffprobecmd.insert(2, proxy)

    with subprocess.Popen(
        ffprobecmd,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    ) as ffprobe_sb:
        ffprobe_sb.communicate()
        if ffprobe_sb.returncode == 0:
            return True
        else:
            return False


def testMacs(portalId, channelId):
    
    # Portal data
    portals = getPortals()
    portal = portals.get(portalId)
    
    portalName = portal.get("name")
    url = portal.get("url")
    macs = list(portal["macs"].keys())

    proxy = portal.get("proxy")
    
    
    workingMacs = []
    brokenMacs = []
    occupiedMacs = []
    for mac in macs:
        macIsFree = isMacFree(portalId, mac)
        channelName, link = getChannelByMac(portalId, channelId, mac)
        if not macIsFree:
            occupiedMacs.append(mac)
        if link:
            if testStream(link, proxy):
                workingMacs.append(mac)
            else:
                brokenMacs.append(mac)

    return workingMacs, brokenMacs, occupiedMacs

# Webinterface routes
@app.route("/", methods=["GET"])
@authorise
def home():
    return redirect("/portals", code=302)


@app.route("/portals", methods=["GET"])
@authorise
def portals():
    """
    Route to display the portal configuration page.
    """
    return render_template("portals.html", portals=getPortals())


@app.route("/portal/add", methods=["POST"])
@authorise
@app.route("/portal/add", methods=["POST"])
@authorise
def portals_add():
    """
    Adds a new portal configuration.
    Accepts both form-data (from the HTML modal) and JSON (for API use).
    """
    logger.debug("Received request to /portal/add. is_json=%s, form_keys=%s", request.is_json, list(request.form.keys()))

    name = url = proxy = streams_per_mac = epg_time_offset = time_zone = None
    macs = []

    if request.is_json:
        data = request.get_json(silent=True) or {}
        logger.debug("JSON payload for /portal/add: %r", data)

        name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        proxy = (data.get("proxy") or "").strip()
        # allow both "streams per mac" and "streams_per_mac"
        streams_per_mac = str(data.get("streams per mac") or data.get("streams_per_mac") or "1")
        epg_time_offset = str(data.get("epg time offset") or data.get("epg_time_offset") or "0")
        time_zone = (data.get("time_zone") or data.get("timeZone") or "").strip()

        raw_macs = data.get("macs", [])
        # macs can be list or string
        if isinstance(raw_macs, str):
            try:
                macs = json.loads(raw_macs)
            except Exception:
                # Fallback: split by separators
                macs = [m.strip() for m in re.split(r"[;,\s]+", raw_macs) if m.strip()]
        elif isinstance(raw_macs, list):
            macs = raw_macs
        else:
            macs = []

    else:
        # HTML form from portals.html sends multipart/form-data
        name = (request.form.get("name") or "").strip()
        url = (request.form.get("url") or "").strip()
        proxy = (request.form.get("proxy") or "").strip()
        streams_per_mac = request.form.get("streams per mac") or "1"
        epg_time_offset = request.form.get("epg time offset") or "0"
        time_zone = (request.form.get("time_zone") or "").strip()

        macs_data = request.form.get("macs", "[]") or "[]"
        logger.debug("Form macs raw value for /portal/add: %r", macs_data)
        try:
            macs = json.loads(macs_data)
        except json.JSONDecodeError:
            error_message = f"Error getting MAC data for Portal({name})"
            logger.error(error_message)
            return jsonify({ "error": error_message }), 400

        # If still empty, try to salvage from a raw textarea if present
        if not macs:
            raw_text = request.form.get("macInput", "")
            if raw_text:
                logger.debug("Falling back to macInput textarea for /portal/add")
                macs = [m.strip() for m in re.split(r"[;,\s]+", raw_text) if m.strip()]

    # Normalise MAC addresses
    macs = [m.upper() for m in macs if m]

    logger.debug("Parsed portal-add fields: name=%r, url=%r, proxy=%r, time_zone=%r, streams_per_mac=%r, epg_time_offset=%r, mac_count=%d",
                 name, url, proxy, time_zone, streams_per_mac, epg_time_offset, len(macs))

    # Check name, url and macs
    if not name or not url or not macs:
        error_message = "Can't add Portal. Name, URL and MACs are required"
        logger.error(error_message)
        return jsonify({"error": error_message}), 400

    # Validate and retrieve the URL
    if not url.endswith(".php"):
        url = stb.getUrl(url, proxy)
        if not url:
            error_message = f"Error getting URL for Portal({name})"
            logger.error(error_message)
            return jsonify({"error": error_message}), 400

    # Create new Portal
    portal = {
        "enabled": "true",
        "name": name,
        "url": url,
        "macs": [],
        "streams per mac": streams_per_mac,
        "epgTimeOffset": epg_time_offset,
        "time_zone": time_zone,
        "proxy": proxy,
    }
    # Add MACs
    portal = portal_update_macs(portal, macs=macs)

    # Add Default settings
    for setting, default in defaultPortal.items():
        if setting not in portal:
            portal[setting] = default

    if len(portal["macs"]) > 0:
        # Save new portal
        portals = getPortals()
        portals[uuid.uuid4().hex] = portal
        savePortals(portals)

        logger.info(f"Portal({portal['name']}) added!")
        return jsonify({"success": f"Portal({portal['name']}) successfully added!"}), 200
    else:
        error_message = f"None of the MACs tested OK for Portal({name}). Adding not successful"
        logger.error(error_message)
        return jsonify({"error": error_message}), 400
@app.route("/portal/checkmacs", methods=["POST"])
@authorise
def portal_checkmacs():
    if request.is_json:
        # Handling the JSON (AJAX) request
        data = request.get_json()
        id = data.get("id")
        name = data.get("name")
        url = data.get("url")
        proxy = data.get("proxy")
        time_zone = data.get("time_zone")

        new_macs = data.get("macs")

        # Validate and retrieve the URL
        if not url.endswith(".php"):
            url = stb.getUrl(url, proxy)
            if not url:
                return jsonify({"error": "Invalid URL"}), 400

        # Test MAC addresses
        valid_macs, dead_macs = test_mac_addresses(url, proxy, new_macs, name, time_zone)

        # Return the tested MACs in JSON format
        return jsonify({"validMacs": valid_macs})
    else:
        # Show message if the request is not JSON
        return jsonify({"error": "Invalid request"}), 400


@app.route("/portal/addmacs", methods=["POST"])
@authorise
def portal_addmacs():
    name = request.form.get("name")
    id = request.form.get("id")
    url = request.form.get("url")
    proxy = request.form.get("proxy")

    macs = json.loads(request.form.get("macs", "[]"))

    # Update portal with new data
    portals = getPortals()
    portal = portals[id]

    # If url or proxy has changed, retest all MACs
    if portal["url"] != url or portal["proxy"] != proxy:
        retest = True
    else:
        retest = False

    # Update MACs based on retest option
    portal = portal_update_macs(portal, macs=macs, retest=retest)

    # Save updated portal if MACs are valid
    if len(portal["macs"]) > 0:
        savePortals(portals)
        logger.info(f"Successfully added MACs to Portal ""{name}""!")
    else:
        logger.error(f"No MACs tested OK for Portal({name}). Update skipped!")
        flash(f"No MACs tested OK for Portal({name}). Update skipped!", "danger")

    return 302


@app.route("/portal/update", methods=["POST", "GET"])
@authorise
def portal_update():
    """
    Updates the portal configuration, with optional retest of all MAC addresses.
    """
    # Handling the form submission (POST) or button call with query parameter (GET)
    id = request.args.get("id") or request.form.get("id")
    retest = request.args.get("retest", "false").lower() == "true"  # Check for 'retest' in URL

    # Retrieve form data (only for POST request)
    enabled = request.form.get("enabled", "false")
    name = request.form.get("name")
    url = request.form.get("url")
    proxy = request.form.get("proxy")
    streams_per_mac = request.form.get("streams per mac")
    epg_time_offset = request.form.get("epg time offset")
    time_zone = request.form.get("time_zone")
    macs = json.loads(request.form.get("macs", "[]"))
    
    # Update portal with new data
    portals = getPortals()
    portal = portals[id]

    # If url or proxy has changed, retest all MACs
    if portal["url"] != url or portal["proxy"] != proxy:
        retest = True

    portal["enabled"] = enabled
    portal["name"] = name
    portal["url"] = url
    portal["proxy"] = proxy
    portal["streams per mac"] = streams_per_mac
    portal["epgTimeOffset"] = epg_time_offset
    portal["time_zone"] = time_zone

    # Update MACs based on retest option
    portal = portal_update_macs(portal, macs=macs, retest=retest)

    # Save updated portal if MACs are valid
    if len(portal["macs"]) > 0:
        savePortals(portals)
        logger.info(f"Portal({name}) updated!")
        flash(f"Portal({name}) updated!", "success")
    else:
        logger.error(f"None of the MACs tested OK for Portal({name}). Update skipped!")
        flash(f"None of the MACs tested OK for Portal({name}). Update skipped!", "danger")

    return redirect("/portals", code=302)


@app.route("/portal/remove", methods=["POST", "GET"])
@authorise
def portal_remove():
    """
    Removes a portal.
    """
    # Handling the form submission (POST) or button call with query parameter (GET)
    id = request.args.get("id") or request.form.get("id")

    portals = getPortals()
    name = portals[id]["name"]
    del portals[id]
    savePortals(portals)
    logger.info(f"Portal ({name}) removed!")
    flash(f"Portal ({name}) removed!", "success")
    return redirect("/portals", code=302)

@app.route("/editor", methods=["GET"])
@authorise
def editor():
    return render_template("editor.html")


@app.route("/editor_data", methods=["GET"])
@authorise
def editor_data():
    channels = []
    portals = getPortals()
    for portal in portals:
        if portals[portal]["enabled"] == "true":
            portalName = portals[portal]["name"]
            url = portals[portal]["url"]
            macs = list(portals[portal]["macs"].keys())
            proxy = portals[portal]["proxy"]
            time_zone = portals[portal]["time_zone"]
            enabledChannels = portals[portal].get("enabled channels", [])
            customChannelNames = portals[portal].get("custom channel names", {})
            customGenres = portals[portal].get("custom genres", {})
            customChannelNumbers = portals[portal].get("custom channel numbers", {})
            customEpgIds = portals[portal].get("custom epg ids", {})
            fallbackChannels = portals[portal].get("fallback channels", {})

            for mac in macs:
                try:
                    token = stb.getToken_fb(url, mac, proxy, time_zone)
                    stb.getProfile(url, mac, token, proxy, time_zone)
                    allChannels = stb.getAllChannels(url, mac, token, proxy, time_zone)
                    genres = stb.getGenreNames(url, mac, token, proxy, time_zone)
                    break
                except:
                    allChannels = None
                    genres = None

            if allChannels and genres:
                for channel in allChannels:
                    channelId = str(channel["id"])
                    channelName = str(channel["name"])
                    channelNumber = str(channel["number"])
                    genre = str(genres.get(str(channel["tv_genre_id"])))
                    if channelId in enabledChannels:
                        enabled = True
                    else:
                        enabled = False
                    customChannelNumber = customChannelNumbers.get(channelId)
                    if customChannelNumber == None:
                        customChannelNumber = ""
                    customChannelName = customChannelNames.get(channelId)
                    if customChannelName == None:
                        customChannelName = ""
                    customGenre = customGenres.get(channelId)
                    if customGenre == None:
                        customGenre = ""
                    customEpgId = customEpgIds.get(channelId)
                    if customEpgId == None:
                        customEpgId = ""
                    fallbackChannel = fallbackChannels.get(channelId)
                    if fallbackChannel == None:
                        fallbackChannel = ""
                    channels.append(
                        {
                            "portal": portal,
                            "portalName": portalName,
                            "enabled": enabled,
                            "channelNumber": channelNumber,
                            "customChannelNumber": customChannelNumber,
                            "channelName": channelName,
                            "customChannelName": customChannelName,
                            "genre": genre,
                            "customGenre": customGenre,
                            "channelId": channelId,
                            "customEpgId": customEpgId,
                            "fallbackChannel": fallbackChannel,
                            "link": "http://"
                            + host
                            + "/play/"
                            + portal
                            + "/"
                            + channelId
                            + "?web=true",
                        }
                    )
            else:
                logger.error(
                    "Error getting channel data for {}, skipping".format(portalName)
                )
                flash(
                    "Error getting channel data for {}, skipping".format(portalName),
                    "danger",
                )

    data = {"data": channels}

    return flask.jsonify(data)

@app.route("/editor/save", methods=["POST"])
@authorise
def editorSave():
    enabledEdits = json.loads(request.form["enabledEdits"])
    numberEdits = json.loads(request.form["numberEdits"])
    nameEdits = json.loads(request.form["nameEdits"])
    genreEdits = json.loads(request.form["genreEdits"])
    epgEdits = json.loads(request.form["epgEdits"])
    fallbackEdits = json.loads(request.form["fallbackEdits"])
    portals = getPortals()
    for edit in enabledEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        enabled = edit["enabled"]
        if enabled:
            portals[portal].setdefault("enabled channels", [])
            portals[portal]["enabled channels"].append(channelId)
        else:
            portals[portal]["enabled channels"] = list(
                filter((channelId).__ne__, portals[portal]["enabled channels"])
            )

    for edit in numberEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        customNumber = edit["custom number"]
        if customNumber:
            portals[portal].setdefault("custom channel numbers", {})
            portals[portal]["custom channel numbers"].update({channelId: customNumber})
        else:
            portals[portal]["custom channel numbers"].pop(channelId)

    for edit in nameEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        customName = edit["custom name"]
        if customName:
            portals[portal].setdefault("custom channel names", {})
            portals[portal]["custom channel names"].update({channelId: customName})
        else:
            portals[portal]["custom channel names"].pop(channelId)

    for edit in genreEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        customGenre = edit["custom genre"]
        if customGenre:
            portals[portal].setdefault("custom genres", {})
            portals[portal]["custom genres"].update({channelId: customGenre})
        else:
            portals[portal]["custom genres"].pop(channelId)

    for edit in epgEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        customEpgId = edit["custom epg id"]
        if customEpgId:
            portals[portal].setdefault("custom epg ids", {})
            portals[portal]["custom epg ids"].update({channelId: customEpgId})
        else:
            portals[portal]["custom epg ids"].pop(channelId)

    for edit in fallbackEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        channelName = edit["channel name"]
        if channelName:
            portals[portal].setdefault("fallback channels", {})
            portals[portal]["fallback channels"].update({channelId: channelName})
        else:
            portals[portal]["fallback channels"].pop(channelId)

    savePortals(portals)
    logger.info("Playlist config saved!")
    flash("Playlist config saved!", "success")

    return redirect("/editor", code=302)


@app.route("/editor/reset", methods=["POST"])
@authorise
def editorReset():
    portals = getPortals()
    for portal in portals:
        portals[portal]["enabled channels"] = []
        portals[portal]["custom channel numbers"] = {}
        portals[portal]["custom channel names"] = {}
        portals[portal]["custom genres"] = {}
        portals[portal]["custom epg ids"] = {}
        portals[portal]["fallback channels"] = {}

    savePortals(portals)
    logger.info("Playlist reset!")
    flash("Playlist reset!", "success")

    return redirect("/editor", code=302)


@app.route("/settings", methods=["GET"])
@authorise
def settings():
    settings = getSettings()
    return render_template(
        "settings.html", settings=settings, defaultSettings=defaultSettings
    )


@app.route("/settings/save", methods=["POST"])
@authorise
def save():
    settings = {}

    for setting, _ in defaultSettings.items():
        value = request.form.get(setting, "false")
        settings[setting] = value

    saveSettings(settings)
    logger.info("Settings saved!")
    flash("Settings saved!", "success")
    return redirect("/settings", code=302)


@app.route("/playlist", methods=["GET"])
@authorise
def playlist():
    # Initialize the list to store channel information
    channels = []
    portals = getPortals()

    # Iterate over all portals
    for portal_name, portal_data in portals.items():
        if portal_data["enabled"] != "true":
            continue

        enabled_channels = portal_data.get("enabled channels", [])
        if not enabled_channels:
            continue

        # Extract portal-specific settings
        name = portal_data["name"]
        url = portal_data["url"]
        macs = list(portal_data["macs"].keys())
        proxy = portal_data["proxy"]
        time_zone = portal_data["time_zone"]
        custom_channel_names = portal_data.get("custom channel names", {})
        custom_genres = portal_data.get("custom genres", {})
        custom_channel_numbers = portal_data.get("custom channel numbers", {})
        custom_epg_ids = portal_data.get("custom epg ids", {})

        # Retrieve channel and genre data for the first valid MAC
        all_channels, genres = None, None
        for mac in macs:
            try:
                token = stb.getToken_fb(url, mac, proxy, time_zone)
                stb.getProfile(url, mac, token, proxy, time_zone)
                all_channels = stb.getAllChannels(url, mac, token, proxy, time_zone)
                genres = stb.getGenreNames(url, mac, token, proxy, time_zone)
                break  # Exit the loop if data retrieval succeeds
            except Exception as e:
                logger.warning(f"Failed to retrieve data for MAC {mac}: {e}")
                continue

        # Skip processing if channel or genre data is unavailable
        if not all_channels or not genres:
            logger.error(f"Error making playlist for {name}, skipping")
            continue

        # Process each channel in the portal
        for channel in all_channels:
            channel_id = str(channel.get("id"))
            if channel_id not in enabled_channels:
                continue

            # Retrieve channel attributes with fallbacks
            channel_name = custom_channel_names.get(channel_id, channel.get("name"))
            genre_id = str(channel.get("tv_genre_id"))
            genre = custom_genres.get(channel_id, genres.get(genre_id))
            channel_number = custom_channel_numbers.get(channel_id, channel.get("number"))
            epg_id = custom_epg_ids.get(channel_id, f"{portal_name}{channel_id}")
            logo_url = channel.get("logo")

            # Build the playlist entry
            use_channel_numbers = getSettings().get("use channel numbers", "true") == "true"
            use_channel_genres = getSettings().get("use channel genres", "true") == "true"

            entry = (
                f"#EXTINF:-1 tvg-id=\"{epg_id}\""
                + (f' tvg-chno="{channel_number}"' if use_channel_numbers and channel_number else "")
                + (f' tvg-logo="{logo_url}"' if logo_url else "")
                + (f' group-title="{genre}"' if use_channel_genres and genre else "")
                + f', {channel_name}\nhttp://{host}/play/{portal_name}/{channel_id}'
            )
            channels.append(entry)

    # Sort the playlist based on user settings
    if getSettings().get("sort playlist by channel name", "true") == "true":
        channels.sort(key=lambda x: x.split(",")[1].split("\n")[0])
    if getSettings().get("use channel numbers", "true") == "true" and \
       getSettings().get("sort playlist by channel number", "false") == "true":
        channels.sort(key=lambda x: x.split('tvg-chno="')[1].split('"')[0])
    if getSettings().get("use channel genres", "true") == "true" and \
       getSettings().get("sort playlist by channel genre", "false") == "true":
        channels.sort(key=lambda x: x.split('group-title="')[1].split('"')[0])

    # Combine all channels into a single playlist
    playlist = "#EXTM3U\n" + "\n".join(channels)

    # Return the playlist as a plain text response
    return Response(playlist, mimetype="text/plain")



@app.route("/xmltv", methods=["GET"])
@authorise
def xmltv():
    
    def float_to_time_stamp(decimal_hours):
        hours = int(decimal_hours)
        minutes = int((decimal_hours - hours) * 60)
        
        sign = '+' if hours >= 0 else '-'
        hours = abs(hours)
        
        return f"{sign}{hours:02d}{minutes:02d}"
    
    channels = ET.Element("tv")
    programmes = ET.Element("tv")
    portals = getPortals()
    for portal in portals:
        if portals[portal]["enabled"] == "true":
            enabledChannels = portals[portal].get("enabled channels", [])
            if len(enabledChannels) != 0:
                name = portals[portal]["name"]
                url = portals[portal]["url"]
                macs = list(portals[portal]["macs"].keys())
                proxy = portals[portal]["proxy"]
                epgTimeOffset = float(portals[portal]["epgTimeOffset"])
                time_zone = portals[portal]["time_zone"]
                customChannelNames = portals[portal].get("custom channel names", {})
                customEpgIds = portals[portal].get("custom epg ids", {})

                for mac in macs:
                    try:
                        token = stb.getToken_fb(url, mac, proxy, time_zone)
                        stb.getProfile(url, mac, token, proxy, time_zone)
                        allChannels = stb.getAllChannels(url, mac, token, proxy, time_zone)
                        epg = stb.getEpg(url, mac, token, 24, proxy, time_zone)
                        break
                    except:
                        allChannels = None
                        epg = None

                if allChannels and epg:
                    for c in allChannels:
                        try:
                            channelId = c.get("id")
                            if str(channelId) in enabledChannels:
                                channelName = customChannelNames.get(str(channelId))
                                if channelName == None:
                                    channelName = str(c.get("name"))
                                epgId = customEpgIds.get(channelId)
                                if epgId == None:
                                    epgId = portal + channelId
                                channelEle = ET.SubElement(
                                    channels, "channel", id=epgId
                                )
                                ET.SubElement(
                                    channelEle, "display-name"
                                ).text = channelName
                                ET.SubElement(channelEle, "icon", src=c.get("logo"))
                                for p in epg.get(channelId):
                                    try:
                                        start = (
                                            datetime.utcfromtimestamp(
                                                p.get("start_timestamp")
                                            ).strftime("%Y%m%d%H%M%S")
                                            + " " + float_to_time_stamp(epgTimeOffset)
                                        )
                                        stop = (
                                            datetime.utcfromtimestamp(
                                                p.get("stop_timestamp")
                                            ).strftime("%Y%m%d%H%M%S")
                                            + " " + float_to_time_stamp(epgTimeOffset)
                                        )
                                        programmeEle = ET.SubElement(
                                            programmes,
                                            "programme",
                                            start=start,
                                            stop=stop,
                                            channel=epgId,
                                        )
                                        ET.SubElement(
                                            programmeEle, "title"
                                        ).text = p.get("name")
                                        ET.SubElement(
                                            programmeEle, "desc"
                                        ).text = p.get("descr")
                                    except:
                                        pass
                        except:
                            pass
                else:
                    logger.error("Error making XMLTV for {}, skipping".format(name))

    xmltv = channels
    for programme in programmes.iter("programme"):
        xmltv.append(programme)

    return Response(
        ET.tostring(xmltv, encoding="unicode", xml_declaration=True),
        mimetype="text/xml",
    )


@app.route("/play/<portalId>/<channelId>", methods=["GET"])
def channel(portalId, channelId):
    def genFfmpegCmd():
        if web:
            ffmpegcmd = [
                "ffmpeg",
                "-loglevel",
                "panic",
                "-hide_banner",
                "-i",
                link,
                "-vcodec",
                "copy",
                "-f",
                "mp4",
                "-movflags",
                "frag_keyframe+empty_moov",
                "pipe:",
            ]
            if proxy:
                ffmpegcmd.insert(1, "-http_proxy")
                ffmpegcmd.insert(2, proxy)
        else:
            ffmpegcmd = str(getSettings()["ffmpeg command"])
            ffmpegcmd = ffmpegcmd.replace("<url>", link)
            ffmpegcmd = ffmpegcmd.replace(
                "<timeout>",
                str(int(getSettings()["stream timeout"]) * int(1000000)),
            )
            if proxy:
                ffmpegcmd = ffmpegcmd.replace("<proxy>", proxy)
            else:
                ffmpegcmd = ffmpegcmd.replace("-http_proxy <proxy>", "")
            " ".join(ffmpegcmd.split())  # cleans up multiple whitespaces
            ffmpegcmd = ffmpegcmd.split()
        return ffmpegcmd

    def streamData():
        
        def occupy():
            occupied.setdefault(portalId, [])
            occupied.get(portalId, []).append(
                {
                    "mac": mac,
                    "channel id": channelId,
                    "channel name": channelName,
                    "client": ip,
                    "portal name": portalName,
                    "start time": startTime,
                }
            )
            logger.info("Occupied Portal({}):MAC({})".format(portalId, mac))

        def unoccupy():
            occupied.get(portalId, []).remove(
                {
                    "mac": mac,
                    "channel id": channelId,
                    "channel name": channelName,
                    "client": ip,
                    "portal name": portalName,
                    "start time": startTime,
                }
            )
            logger.info("Unoccupied Portal({}):MAC({})".format(portalId, mac))

        def calcStreamDuration():
            # calc streaming duration
            streamDuration = datetime.now(timezone.utc).timestamp() - startTime 
            return streamDuration

        def startffmpeg():
            nonlocal streamCanceled
            def read_stderr(ffmpeg_sp, last_stderr):
                while ffmpeg_sp.poll() is None:
                    try:
                        line = ffmpeg_sp.stderr.readline()
                    except Exception as e:
                        break
                    if not line:
                        break
                    # Decode new line and keep latest 10 lines
                    stderr_text = line.decode('utf-8').strip()
                    logger.debug("FFMPEG stderr: " + stderr_text)
                    last_stderr.append(stderr_text)
                    if len(last_stderr) > 10: 
                        last_stderr.pop(0) 
            
            last_stderr = [] # list to save the last stderr output
            try:
                with subprocess.Popen(
                    ffmpegcmd,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                ) as ffmpeg_sp:
                    # Start reading stderr of ffmpeg in seperate thread
                    stderr_thread = threading.Thread(target=read_stderr, args=(ffmpeg_sp, last_stderr))
                    stderr_thread.start()

                    while True:
                        # read ffmpeg stdout buffer
                        chunk = ffmpeg_sp.stdout.read(bufferSize)

                        if len(chunk) == 0:
                            logger.info("No streaming data from ffmpeg detected.")
                            if ffmpeg_sp.poll() is not None:
                                logger.debug("Ffmpeg process closed unexpectedly with return / error code ({}).".format(str(ffmpeg_sp.poll())))
                                # Check errors
                                error_text = "\n".join(last_stderr)
                                if "Operation timed out" in error_text:
                                    logger.error("Stream to client ({}) from Portal ({}) timed out.".format(ip, portalName))
                                elif "I/O error" in error_text:
                                    # stream ended / closed by server
                                    logger.error("Stream to client ({}) from Portal ({}) was closed.".format(ip, portalName))
                                    streamCanceled = True
                                else:
                                    logger.error("Stream to client ({}) from Portal ({}) stopped with unknown error in ffmpeg process.".format(ip, portalName))
                                    logger.debug("Ffmpeg error:\n{}".format(error_text))
                                    streamCanceled = True
                                # stop streaming
                                break
                        yield chunk
            except Exception as e:
                logger.error("Stream request to URL ({}) ended with error:\n{}".format(link, e))
                streamCanceled = True
                pass
            finally:              
                if stderr_thread.is_alive():
                    stderr_thread.join(timeout=0.2)  # Wait for the end of the stderr thread
                ffmpeg_sp.kill()
                
        def startdirectbuffer():
            nonlocal streamCanceled
            try:
                # Send a request to the source video stream URL
                reqTimeout = int(getSettings()["stream timeout"]) # Request timeout in seconds
                response = requests.get(link, stream=True, timeout=reqTimeout)

                # Check if the request was successful
                # Status Code Decryption: 200, 301, 302, 405, 406, 403 (Possible) = Server is broadcasting | 401, 404, 458 = Server is not broadcasting or additionally protected /Banned /GEO | 500 = Server error. (To determine pre-broadcasts)
                if response.status_code == 200:
                    # Start Streaming
                    for chunk in response.iter_content(chunk_size=bufferSize):
                        if len(chunk) == 0:
                            logger.info("No streaming data.")
                            return
                        yield chunk
                else:
                    logger.error("Couldn't connect to stream URL ({}).\n Request stopped with status code ({}).".format(link, response.status_code))
            except requests.exceptions.Timeout:
                logger.error("Stream request to URL ({}) timed out.".format(link))
                return
            except requests.exceptions.RequestException as e:
                logger.error("Stream request to URL ({}) ended with error:\n{}".format(link, e))
                pass
            except Exception as e:
                logger.error("Stream from direct buffer raised an unknown error:\n{}".format(e))
                pass
                
            # stream ended / closed by server
            streamCanceled = True
            logger.info("Stream to client ({}) from Portal ({}) was closed.".format(ip, portalName))
            
        # Start new stream
        startTime = datetime.now(timezone.utc).timestamp()
        streamCanceled = False
        try:
            occupy()
            if web or getSettings().get("stream method", "ffmpeg") == "ffmpeg":
                # Generate specific ffmpeg command
                ffmpegcmd = genFfmpegCmd()
                
                logger.debug("Start Stream by ffmpeg.")
                for chunk in startffmpeg():
                    yield chunk
            elif getSettings().get("stream method", "buffer") == "buffer":
                logger.debug("Start Stream by direct buffer.")
                for chunk in startdirectbuffer():
                    yield chunk
            else:
                logger.error("Unknown streaming method.")
        except GeneratorExit:
            logger.info('Stream closed by client.')
            pass
        except Exception as e:
            pass
        finally:
            unoccupy()
            streamDuration = round(calcStreamDuration(), 1)
            # update statistics
            portal["macs"][mac]["stats"]["playtime"] += streamDuration
            portal["macs"][mac]["stats"]["errors"] += streamCanceled
            # move Mac if stream was canceled by server after a short streaming period (over-usage indication)
            if streamCanceled and streamDuration <= 60:
                logger.info("A forced disconnection by the server after a short streaming time indicates that mac address might be over-used.")
                moveMac(portalId, mac)
            savePortals(portals)

    # client info from request
    web = request.args.get("web")
    ip = request.remote_addr

    # Portal data
    portals = getPortals()
    portal = portals.get(portalId)
    
    portalName = portal.get("name")
    url = portal.get("url")
    macs = list(portal["macs"].keys())
    streamsPerMac = int(portal.get("streams per mac"))
    time_zone = portal.get("time_zone")
    proxy = portal.get("proxy")

    logger.info(
        "IP({}) requested Portal({}):Channel({})".format(ip, portalId, channelId)
    )
    with lock:
        channelName, link, mac = getChannel(portalId, channelId)
        if link:
            if getSettings().get("test streams", "true") == "false" or testStream(link, proxy):
                # update statistics
                portal["macs"][mac]["stats"]["requests"] += 1
                savePortals(portals)
                
                # start streaming
                if getSettings().get("stream method", "ffmpeg") != "redirect":
                    return Response(
                        stream_with_context(streamData()), mimetype="application/octet-stream"
                    )
                else:
                    logger.info("Redirect sent")
                    return redirect(link)
                
    logger.info(
        "Unable to connect to Portal({}) using MAC({})".format(portalId, mac)
    )

    # Look for fallback 
    if not web:
        logger.info(
            "Portal({}):Channel({}) is not working. Looking for fallbacks...".format(
                portalId, channelId
            )
        )

        for fportal in portals.values():
            if fportal["enabled"] == "true":
                fportalId = fportal.get("id")
                proxy = fportal.get("proxy")
                fallbackChannels = fportal.get("fallback channels")
                if channelName in fallbackChannels.values():
                    for k, v in fallbackChannels.items():
                        if v == channelName:
                            with lock:
                                fChannelId = k
                                fChannelName = v
                                try:
                                    channelName, link, mac = getChannel(fportalId, fChannelId)
                                except:
                                    link = None
                                    channelName = None
                                    logger.info(
                                        "Unable to connect to fallback Portal({})".format(fportalId)
                                    )
                                if link:
                                    if testStream(link, proxy):
                                        logger.info(
                                            "Fallback found for Portal({}):Channel({})".format(fportalId, fChannelId)
                                        )
                                        # update statistics
                                        fportal["macs"][mac]["stats"]["requests"] += 1
                                        savePortals(portals)
                                        
                                        # start streaming
                                        if getSettings().get("stream method", "ffmpeg") != "redirect":
                                            return Response(
                                                stream_with_context(streamData()),
                                                mimetype="application/octet-stream",
                                            )
                                        else:
                                            logger.info("Redirect sent")
                                            return redirect(link)

    logger.info(
        "No fallback found for Portal({}):Channel({})".format(portalId, channelId)
    )

    # No stream available
    return make_response("No streams available", 503)


@app.route("/dashboard")
@authorise
def dashboard():
    return render_template("dashboard.html")


@app.route("/streaming")
@authorise
def streaming():
    return flask.jsonify(occupied)


@app.route("/log")
@authorise
def log():
    with open("STB-Proxy.log") as f:
        log = f.read()
    return log


# HD Homerun #
def hdhr(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        auth = request.authorization
        settings = getSettings()
        security = settings["enable security"]
        username = settings["username"]
        password = settings["password"]
        hdhrenabled = settings["enable hdhr"]
        if (
            security == "false"
            or auth
            and auth.username == username
            and auth.password == password
        ):
            if hdhrenabled:
                return f(*args, **kwargs)
        return make_response("Error", 404)

    return decorated


@app.route("/discover.json", methods=["GET"])
@hdhr
def discover():
    settings = getSettings()
    name = settings["hdhr name"]
    id = settings["hdhr id"]
    tuners = settings["hdhr tuners"]
    data = {
        "BaseURL": host,
        "DeviceAuth": name,
        "DeviceID": id,
        "FirmwareName": "STB-Proxy",
        "FirmwareVersion": "1337",
        "FriendlyName": name,
        "LineupURL": host + "/lineup.json",
        "Manufacturer": "Chris",
        "ModelNumber": "1337",
        "TunerCount": int(tuners),
    }
    return flask.jsonify(data)


@app.route("/lineup_status.json", methods=["GET"])
@hdhr
def status():
    data = {
        "ScanInProgress": 0,
        "ScanPossible": 0,
        "Source": "Antenna",
        "SourceList": ["Antenna"],
    }
    return flask.jsonify(data)


@app.route("/lineup.json", methods=["GET"])
@app.route("/lineup.post", methods=["POST"])
@hdhr
def lineup():
    lineup = []
    portals = getPortals()
    for portal in portals:
        if portals[portal]["enabled"] == "true":
            enabledChannels = portals[portal].get("enabled channels", [])
            if len(enabledChannels) != 0:
                name = portals[portal]["name"]
                url = portals[portal]["url"]
                macs = list(portals[portal]["macs"].keys())
                proxy = portals[portal]["proxy"]
                time_zone = portals[portal]["time_zone"]
                customChannelNames = portals[portal].get("custom channel names", {})
                customChannelNumbers = portals[portal].get("custom channel numbers", {})

                for mac in macs:
                    try:
                        token = stb.getToken_fb(url, mac, proxy, time_zone)
                        stb.getProfile(url, mac, token, proxy, time_zone)
                        allChannels = stb.getAllChannels(url, mac, token, proxy, time_zone)
                        break
                    except:
                        allChannels = None

                if allChannels:
                    for channel in allChannels:
                        channelId = str(channel.get("id"))
                        if channelId in enabledChannels:
                            channelName = customChannelNames.get(channelId)
                            if channelName == None:
                                channelName = str(channel.get("name"))
                            channelNumber = customChannelNumbers.get(channelId)
                            if channelNumber == None:
                                channelNumber = str(channel.get("number"))

                            lineup.append(
                                {
                                    "GuideNumber": channelNumber,
                                    "GuideName": channelName,
                                    "URL": "http://"
                                    + host
                                    + "/play/"
                                    + portal
                                    + "/"
                                    + channelId,
                                }
                            )
                else:
                    logger.error("Error making lineup for {}, skipping".format(name))

    return flask.jsonify(lineup)


if __name__ == "__main__":
    config = loadConfig()
    if debugMode or ("TERM_PROGRAM" in os.environ.keys() and os.environ["TERM_PROGRAM"] == "vscode"):
        # If DEBUG is active or code running In VS Code, use default flask development sever in debug mode
        logger.info("ATTENTION: Server started in debug mode. Don't use on productive systems!")
        app.run(host="0.0.0.0", port=8001, debug=True, use_reloader=True)
        # Note: Flask server in debug mode can lead to errors in vscode debugger ([errno 98] address in use)
        #app.run(host="0.0.0.0", port=8001, debug=False)
    else:
        # On release use waitress server with multi-threading
        waitress.serve(app, port=8001, _quiet=True, threads=24)



@app.route("/playlists", methods=["GET"])
@authorise
def playlists_page():
    """Playlists overview with a robust template fallback."""
    portals = getPortals()
    def _is_truthy(v):
        return str(v).strip().lower() in {"true","1","yes","on"}
    portals = {pid: p for pid,p in portals.items() if _is_truthy(p.get("enabled"))}
    portals = dict(sorted(portals.items(), key=lambda kv: (kv[1].get("name") or kv[0])))
    try:
        return render_template("playlists.html", portals=portals)
    except Exception:
        rows = []
        for pid, p in portals.items():
            name = p.get("name") or pid
            url = p.get("url") or ""
            rows.append(
                '<li><strong>' + name + '</strong> '
                '<small style="color:#666">' + url + '</small> '
                '<a href="/playlist/' + pid + '" target="_blank">Anzeigen</a> '
                '<a href="/playlist/' + pid + '.m3u" target="_blank">Download .m3u</a>'
                '</li>'
            )
        html = "<html><head><title>Playlists</title></head><body><h2>Playlists</h2><ul>" + "\n".join(rows) + "</ul></body></html>"

@app.route("/playlist/<portalId>", methods=["GET"])
@app.route("/playlist/<portalId>.m3u", methods=["GET"])
@authorise
def playlist_portal(portalId):
    portals = getPortals()
    portal = portals.get(portalId)
    if not portal or str(portal.get("enabled")).strip().lower() not in {"true","1","yes","on"}:
        return Response("#EXTM3U\n", mimetype="text/plain")
    name = portal.get("name")
    url = portal.get("url")
    macs = list(portal.get("macs", {}).keys())
    proxy = portal.get("proxy")
    time_zone = portal.get("time_zone")
    enabled_channels = portal.get("enabled channels", [])
    if not macs or not enabled_channels:
        return Response("#EXTM3U\n", mimetype="text/plain")
    all_channels, genres = None, None
    for mac in macs:
        try:
            token = stb.getToken_fb(url, mac, proxy, time_zone)
            stb.getProfile(url, mac, token, proxy, time_zone)
            all_channels = stb.getAllChannels(url, mac, token, proxy, time_zone)
            genres = stb.getGenreNames(url, mac, token, proxy, time_zone)
            if all_channels and genres:
                break
        except Exception:
            continue
    if not all_channels or not genres:
        return Response("#EXTM3U\n", mimetype="text/plain")
    custom_channel_names = portal.get("custom channel names", {})
    custom_genres = portal.get("custom genres", {})
    custom_channel_numbers = portal.get("custom channel numbers", {})
    custom_epg_ids = portal.get("custom epg ids", {})
    use_channel_numbers = getSettings().get("use channel numbers", "true") == "true"
    use_channel_genres = getSettings().get("use channel genres", "true") == "true"
    entries = []
    for ch in all_channels:
        cid = str(ch.get("id"))
        if cid not in enabled_channels:
            continue
        channel_name = custom_channel_names.get(cid, ch.get("name"))
        genre_id = str(ch.get("tv_genre_id"))
        genre = custom_genres.get(cid, genres.get(genre_id))
        channel_number = custom_channel_numbers.get(cid, ch.get("number"))
        epg_id = custom_epg_ids.get(cid, f"{portalId}{cid}")
        logo_url = ch.get("logo")
        header = f'#EXTINF:-1 tvg-id="{epg_id}"'
        if use_channel_numbers and channel_number:
            header += f' tvg-chno="{channel_number}"'
        if logo_url:
            header += f' tvg-logo="{logo_url}"'
        if use_channel_genres and genre:
            header += f' group-title="{genre}"'
        play_url = url_for("channel", portalId=portalId, channelId=cid, _external=True)
        entries.append(f"{header}, {channel_name}\n{play_url}")
    # Sorting
    if getSettings().get("sort playlist by channel name", "true") == "true":
        entries.sort(key=lambda x: x.split(",")[1].split("\n")[0])
    if use_channel_numbers and getSettings().get("sort playlist by channel number", "false") == "true":
        import re as _re
        def chan_num(e):
            m = _re.search(r'tvg-chno="(\d+)"', e)
            return int(m.group(1)) if m else 1_000_000
        entries.sort(key=chan_num)
    playlist = "#EXTM3U\n" + "\n".join(entries)
    return Response(playlist, mimetype="text/plain")


@app.route("/playlist/<portal_id>", methods=["GET"])
@app.route("/playlist/<portal_id>.m3u", methods=["GET"])
@authorise
def playlist_portal(portal_id):
    portals = getPortals()
    portal = portals.get(portal_id)
    if not portal or str(portal.get("enabled")).strip().lower() not in {"true","1","yes","on"}:
        return Response("#EXTM3U\n", mimetype="text/plain")
    name = portal.get("name") or portal_id
    url = portal.get("url")
    macs = list(portal.get("macs", {}).keys())
    proxy = portal.get("proxy")
    time_zone = portal.get("time_zone")
    enabled_channels = portal.get("enabled channels", [])
    if not macs or not enabled_channels:
        return Response("#EXTM3U\n", mimetype="text/plain")

    all_channels, genres = None, None
    for mac in macs:
        try:
            token, used_proxy = stb.getToken_fb_multi(url, mac, proxy, time_zone)
            if not token:
                raise Exception("no token")
            pxy = used_proxy or proxy
            stb.getProfile(url, mac, token, pxy, time_zone)
            all_channels = stb.getAllChannels(url, mac, token, pxy, time_zone)
            genres = stb.getGenreNames(url, mac, token, pxy, time_zone)
            if all_channels and genres:
                break
        except Exception:
            continue

    if not all_channels or not genres:
        return Response("#EXTM3U\n", mimetype="text/plain")

    custom_channel_names = portal.get("custom channel names", {})
    custom_genres = portal.get("custom genres", {})
    custom_channel_numbers = portal.get("custom channel numbers", {})
    custom_epg_ids = portal.get("custom epg ids", {})

    use_channel_numbers = getSettings().get("use channel numbers", "true") == "true"
    use_channel_genres = getSettings().get("use channel genres", "true") == "true"

    entries = []
    for ch in all_channels:
        cid = str(ch.get("id"))
        if cid not in enabled_channels:
            continue
        channel_name = custom_channel_names.get(cid, ch.get("name"))
        genre_id = str(ch.get("tv_genre_id"))
        genre = custom_genres.get(cid, genres.get(genre_id))
        channel_number = custom_channel_numbers.get(cid, ch.get("number"))
        epg_id = custom_epg_ids.get(cid, f"{portal_id}{cid}")
        logo_url = ch.get("logo")

        header = f'#EXTINF:-1 tvg-id="{epg_id}"'
        if use_channel_numbers and channel_number:
            header += f' tvg-chno="{channel_number}"'
        if logo_url:
            header += f' tvg-logo="{logo_url}"'
        if use_channel_genres and genre:
            header += f' group-title="{genre}"'

        play_url = url_for("channel", portalId=portal_id, channelId=cid, _external=True)
        entries.append(f"{header}, {channel_name}\n{play_url}")

    # Sorting
    if getSettings().get("sort playlist by channel name", "true") == "true":
        entries.sort(key=lambda x: x.split(",")[1].split("\n")[0])
    if use_channel_numbers and getSettings().get("sort playlist by channel number", "false") == "true":
        import re as _re
        def chan_num(e):
            m = _re.search(r'tvg-chno="(\d+)"', e)
            return int(m.group(1)) if m else 1_000_000
        entries.sort(key=chan_num)

    playlist = "#EXTM3U\n" + "\n".join(entries)
    return Response(playlist, mimetype="text/plain")
