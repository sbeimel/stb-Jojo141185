import sys
import os
import shutil
import time
from datetime import datetime, timedelta, timezone
import xml.etree.ElementTree as ET
import xml.dom.minidom as minidom
import threading
from threading import Thread
import logging

logger = logging.getLogger("MacReplayV2")
logger.setLevel(logging.INFO)
logFormat = logging.Formatter("%(asctime)s [%(levelname)s] %(message)s")

# Docker-optimized paths
if os.getenv("CONFIG"):
    configFile = os.getenv("CONFIG")
    log_dir = os.path.dirname(configFile)
else:
    # Default paths for container
    log_dir = "/app/data"
    configFile = os.path.join(log_dir, "MacReplayV2.json")

# Create directories if they don't exist
os.makedirs(log_dir, exist_ok=True)
os.makedirs("/app/logs", exist_ok=True)

# Seamlessly migrate legacy MacReplay config filenames to the new MacReplayV2 convention
legacyConfigFile = os.path.join(log_dir, "MacReplay.json")
if not os.path.exists(configFile) and os.path.exists(legacyConfigFile):
    shutil.copy2(legacyConfigFile, configFile)
    logger.info("Legacy MacReplay config detected – migrated to MacReplayV2.json")

# Log file path for container
log_file_path = os.path.join("/app/logs", "MacReplayV2.log")

# Set up logging
fileHandler = logging.FileHandler(log_file_path)
fileHandler.setFormatter(logFormat)
logger.addHandler(fileHandler)

consoleFormat = logging.Formatter("[%(levelname)s] %(message)s")
consoleHandler = logging.StreamHandler()
consoleHandler.setFormatter(consoleFormat)
logger.addHandler(consoleHandler)

# Docker-optimized ffmpeg paths (system-installed)
ffmpeg_path = "ffmpeg"
ffprobe_path = "ffprobe"

# Check if the binaries exist
import subprocess
try:
    subprocess.run([ffmpeg_path, "-version"], capture_output=True, check=True)
    subprocess.run([ffprobe_path, "-version"], capture_output=True, check=True)
    logger.info("FFmpeg and FFprobe found and working")
except (subprocess.CalledProcessError, FileNotFoundError):
    logger.error("Error: ffmpeg or ffprobe not found!")

import flask
from flask import Flask, jsonify
import stb  # deine STB-Handshake-Logik
import json
import subprocess
import uuid
import xml.etree.cElementTree as CET
from flask import (
    Flask,
    render_template,
    redirect,
    request,
    Response,
    make_response,
    flash,
)
from functools import wraps
import secrets
import waitress

app = Flask(__name__)
app.secret_key = secrets.token_urlsafe(32)

# Add custom Jinja2 filter for JSON serialization
@app.template_filter("tojsonfilter")
def tojson_filter(obj):
    return json.dumps(obj)


# Docker-optimized host configuration
if os.getenv("HOST"):
    host = os.getenv("HOST")
else:
    host = "0.0.0.0:8001"
logger.info(f"Server started on http://{host}")

try:
    EPG_REFRESH_INTERVAL_HOURS = float(os.getenv("EPG_REFRESH_INTERVAL_HOURS", 4))
except ValueError:
    logger.warning(
        "Invalid EPG_REFRESH_INTERVAL_HOURS value supplied; defaulting to 4 hours."
    )
    EPG_REFRESH_INTERVAL_HOURS = 4.0

EPG_REFRESH_INTERVAL_SECONDS = max(60, int(EPG_REFRESH_INTERVAL_HOURS * 3600))

logger.info(f"Using config file: {configFile}")

occupied = {}
config = {}
cached_lineup = []
cached_playlist = None
last_playlist_host = None
cached_xmltv = None
last_updated = 0

# optional Vorlage, wird im channel() dynamisch aus Settings gebaut
d_ffmpegcmd = [
    "-re",
    "-http_proxy",
    "<proxy>",
    "-timeout",
    "<timeout>",
    "-i",
    "<url>",
    "-map",
    "0",
    "-codec",
    "copy",
    "-f",
    "mpegts",
    "-flush_packets",
    "0",
    "-fflags",
    "+nobuffer",
    "-flags",
    "low_delay",
    "-strict",
    "experimental",
    "-analyzeduration",
    "0",
    "-probesize",
    "32",
    "-copyts",
    "-threads",
    "12",
    "pipe:",
]

defaultSettings = {
    "stream method": "ffmpeg",
    "ffmpeg command": "-re -http_proxy <proxy> -timeout <timeout> -i <url> -map 0 -codec copy -f mpegts -flush_packets 0 -fflags +nobuffer -flags low_delay -strict experimental -analyzeduration 0 -probesize 32 -copyts -threads 12 pipe:",
    "ffmpeg timeout": "5",
    "test streams": "true",
    "try all macs": "true",
    "use channel genres": "true",
    "use channel numbers": "true",
    "sort playlist by channel genre": "false",
    "sort playlist by channel number": "true",
    "sort playlist by channel name": "false",
    "enable security": "false",
    "username": "admin",
    "password": "12345",
    "enable hdhr": "true",
    "hdhr name": "MacReplayV2",
    "hdhr id": str(uuid.uuid4().hex),
    "hdhr tuners": "10",
}

defaultPortal = {
    "enabled": "true",
    "name": "",
    "url": "",
    "macs": {},
    "streams per mac": "1",
    "epg offset": "0",
    "proxy": "",
    "enabled channels": [],
    "custom channel names": {},
    "custom channel numbers": {},
    "custom genres": {},
    "custom epg ids": {},
    "fallback channels": {},
}


def loadConfig():
    """Load configuration and normalize portal IDs so that
    the portal ID (dictionary key) always matches the portal name.
    """
    try:
        with open(configFile) as f:
            data = json.load(f)
    except Exception:
        logger.warning("No existing config found. Creating a new one")
        data = {}

    data.setdefault("portals", {})
    data.setdefault("settings", {})

    # Normalise settings
    settings = data["settings"]
    settingsOut = {}

    for setting, default in defaultSettings.items():
        value = settings.get(setting)
        if not value or type(default) != type(value):
            value = default
        settingsOut[setting] = value

    data["settings"] = settingsOut

    # Normalise portals and migrate IDs -> use portal name as key
    portals = data["portals"]
    portalsOut = {}

    for old_id, portal_data in portals.items():
        name_value = portal_data.get("name") or old_id
        new_id = str(name_value)

        portalsOut[new_id] = {}
        for setting, default in defaultPortal.items():
            value = portal_data.get(setting)
            if not value or type(default) != type(value):
                value = default
            portalsOut[new_id][setting] = value

        portalsOut[new_id]["name"] = new_id

    data["portals"] = portalsOut

    with open(configFile, "w") as f:
        json.dump(data, f, indent=4)

    return data


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
    macs = portals[portalId]["macs"]
    x = macs[mac]
    del macs[mac]
    macs[mac] = x
    portals[portalId]["macs"] = macs
    savePortals(portals)


# Hilfsfunktion: MACs robust aus String / Liste parsen
def parse_macs_input(raw_macs, from_json=False):
    import re

    mac_list = []

    if from_json and isinstance(raw_macs, list):
        parts = raw_macs
    else:
        if raw_macs is None:
            raw_macs = ""
        parts = str(raw_macs).replace("\r", "").replace("\n", ",").split(",")

    for part in parts:
        m = str(part).strip().upper()
        if not m:
            continue
        m = m.replace("-", ":")
        m = re.sub(r"[^0-9A-F:]", "", m)
        if ":" not in m and len(m) == 12:
            m = ":".join(m[i : i + 2] for i in range(0, 12, 2))
        if re.match(r"^[0-9A-F]{2}(:[0-9A-F]{2}){5}$", m):
            mac_list.append(m)
        else:
            logger.warning(f"Invalid MAC format ignored: '{part}' -> '{m}'")

    seen = set()
    uniq = []
    for m in mac_list:
        if m not in seen:
            seen.add(m)
            uniq.append(m)
    return uniq


@app.route("/", methods=["GET"])
@authorise
def home():
    return redirect("/portals", code=302)


@app.route("/portals", methods=["GET"])
@authorise
def portals():
    """Portalliste inkl. MACs & Expiry aufbereitet für das Template."""
    portals_raw = getPortals()
    now = datetime.now(timezone.utc)
    portals_view = {}

    for portal_id, portal in portals_raw.items():
        macs = portal.get("macs", {})
        macs_list = []

        for mac, expiry in macs.items():
            expired = False
            exp_str = str(expiry)
            exp_dt = None

            try:
                if isinstance(expiry, (int, float)):
                    exp_dt = datetime.fromtimestamp(float(expiry), timezone.utc)
                else:
                    s = str(expiry).strip()
                    # einige mögliche Formate
                    for fmt in (
                        "%Y-%m-%d",
                        "%Y-%m-%d %H:%M:%S",
                        "%d.%m.%Y",
                        "%d.%m.%Y %H:%M",
                        "%Y/%m/%d",
                    ):
                        try:
                            exp_dt = datetime.strptime(s, fmt).replace(tzinfo=timezone.utc)
                            break
                        except ValueError:
                            continue
            except Exception:
                exp_dt = None

            if exp_dt and exp_dt < now:
                expired = True

            macs_list.append(
                {
                    "mac": mac,
                    "expiry": exp_str,
                    "expired": expired,
                }
            )

        portal_copy = dict(portal)
        portal_copy["macs_list"] = macs_list
        portals_view[portal_id] = portal_copy

    return render_template("portals.html", portals=portals_view)


@app.route("/portal/add", methods=["POST"])
@authorise
def portalsAdd():
    """
    Portal hinzufügen.
    - Unterstützt HTML-Form (redirect)
    - und JSON (saubere JSON-Antwort -> kein 'Unexpected token <' mehr).
    """
    global cached_xmltv
    cached_xmltv = None

    is_json = request.is_json
    if is_json:
        data = request.get_json(silent=True) or {}
        name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        proxy = (data.get("proxy") or "").strip()
        streamsPerMac = str(data.get("streams_per_mac") or data.get("streams per mac") or "1")
        epgOffset = str(data.get("epg_offset") or data.get("epg offset") or "0")
        raw_macs = data.get("macs") or []
        macs = parse_macs_input(raw_macs, from_json=True)
    else:
        name = request.form.get("name", "").strip()
        url = request.form.get("url", "").strip()
        proxy = request.form.get("proxy", "").strip()
        streamsPerMac = request.form.get("streams per mac", "1")
        epgOffset = request.form.get("epg offset", "0")
        raw_macs = request.form.get("macs", "")
        macs = parse_macs_input(raw_macs)

    enabled = "true"

    logger.info(f"Add portal request: name={name}, url={url}, macs={macs}")

    if not name or not url or not macs:
        msg = "Name, URL und mindestens eine gültige MAC-Adresse sind erforderlich."
        logger.error(
            f"Can't add Portal. Name, URL and MACs are required (name={name}, url={url}, macs_parsed={macs})"
        )
        if is_json:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect("/portals", code=302)

    # URL ggf. auflösen
    if not url.endswith(".php"):
        resolved = stb.getUrl(url, proxy)
        if not resolved:
            logger.error("Error getting URL for Portal({})".format(name))
            msg = f"Fehler beim Ermitteln der Portal-URL für ({name})"
            if is_json:
                return jsonify({"success": False, "error": msg}), 400
            flash(msg, "danger")
            return redirect("/portals", code=302)
        url = resolved

    macsd = {}
    for mac in macs:
        logger.info(
            f"Testing MAC({mac}) for Portal({name}) via URL {url} (proxy={proxy or 'none'})"
        )
        token = stb.getToken(url, mac, proxy)
        if token:
            stb.getProfile(url, mac, token, proxy)
            expiry = stb.getExpires(url, mac, token, proxy)
            if expiry:
                macsd[mac] = expiry
                logger.info(
                    "Successfully tested MAC({}) for Portal({}) – expiry: {}".format(
                        mac, name, expiry
                    )
                )
                if not is_json:
                    flash(
                        "Successfully tested MAC({}) for Portal({})".format(mac, name),
                        "success",
                    )
                continue

        logger.error("Error testing MAC({}) for Portal({})".format(mac, name))
        if not is_json:
            flash("Error testing MAC({}) for Portal({})".format(mac, name), "danger")

    if len(macsd) > 0:
        portal = {
            "enabled": enabled,
            "name": name,
            "url": url,
            "macs": macsd,
            "streams per mac": streamsPerMac,
            "epg offset": epgOffset,
            "proxy": proxy,
        }

        for setting, default in defaultPortal.items():
            if setting not in portal or portal.get(setting) in (None, ""):
                portal[setting] = default

        portals = getPortals()
        portals[name] = portal  # ID == Name
        savePortals(portals)
        logger.info("Portal({}) added!".format(portal["name"]))

        if is_json:
            return jsonify({"success": True, "portal": {"id": name, "name": name}}), 200
        else:
            return redirect("/portals", code=302)

    else:
        logger.error(
            "None of the MACs tested OK for Portal({}). Adding not successfull".format(
                name
            )
        )
        msg = (
            "Keine der MACs für Portal({}) ist gültig / aktiv. "
            "Portal wurde nicht hinzugefügt.".format(name)
        )
        if is_json:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect("/portals", code=302)


@app.route("/portal/update", methods=["POST"])
@authorise
def portalUpdate():
    """
    Portal aktualisieren.
    - Unterstützt HTML-Form (redirect)
    - und JSON (saubere JSON-Antwort).
    """
    global cached_xmltv
    cached_xmltv = None

    is_json = request.is_json
    if is_json:
        data = request.get_json(silent=True) or {}
        old_id = (data.get("id") or "").strip()
        enabled = "true" if data.get("enabled") in (True, "true", "1") else "false"
        new_name = (data.get("name") or "").strip()
        url = (data.get("url") or "").strip()
        proxy = (data.get("proxy") or "").strip()
        streamsPerMac = str(data.get("streams_per_mac") or data.get("streams per mac") or "1")
        epgOffset = str(data.get("epg_offset") or data.get("epg offset") or "0")
        raw_macs = data.get("macs") or []
        retest = data.get("retest")
        macs = parse_macs_input(raw_macs, from_json=True)
    else:
        old_id = request.form.get("id", "").strip()
        enabled = request.form.get("enabled", "false")
        new_name = request.form.get("name", "").strip()
        url = request.form.get("url", "").strip()
        proxy = request.form.get("proxy", "").strip()
        streamsPerMac = request.form.get("streams per mac", "1")
        epgOffset = request.form.get("epg offset", "0")
        raw_macs = request.form.get("macs", "")
        retest = request.form.get("retest", None)
        macs = parse_macs_input(raw_macs)

    portals = getPortals()
    portal_entry = portals.get(old_id)
    if not portal_entry:
        msg = "Portal nicht gefunden."
        logger.error(f"PortalUpdate: portal with id '{old_id}' not found")
        if is_json:
            return jsonify({"success": False, "error": msg}), 404
        flash(msg, "danger")
        return redirect("/portals", code=302)

    if not new_name or not url or not macs:
        msg = "Name, URL und mindestens eine gültige MAC-Adresse sind erforderlich."
        logger.error(
            f"Can't update Portal. Name, URL and MACs are required "
            f"(name={new_name}, url={url}, macs_parsed={macs})"
        )
        if is_json:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect("/portals", code=302)

    if not url.endswith(".php"):
        resolved = stb.getUrl(url, proxy)
        if not resolved:
            logger.error("Error getting URL for Portal({})".format(new_name))
            msg = f"Fehler beim Ermitteln der Portal-URL für ({new_name})"
            if is_json:
                return jsonify({"success": False, "error": msg}), 400
            flash(msg, "danger")
            return redirect("/portals", code=302)
        url = resolved

    oldmacs = portal_entry.get("macs", {})
    macsout = {}
    deadmacs = []

    for mac in macs:
        logger.info(f"PortalUpdate: checking MAC({mac}) for Portal({new_name})")
        if retest or mac not in oldmacs.keys():
            token = stb.getToken(url, mac, proxy)
            if token:
                stb.getProfile(url, mac, token, proxy)
                expiry = stb.getExpires(url, mac, token, proxy)
                if expiry:
                    macsout[mac] = expiry
                    logger.info(
                        "Successfully tested MAC({}) for Portal({})".format(
                            mac, new_name
                        )
                    )
                    if not is_json:
                        flash(
                            "Successfully tested MAC({}) for Portal({})".format(
                                mac, new_name
                            ),
                            "success",
                        )

            if mac not in list(macsout.keys()):
                deadmacs.append(mac)

        if mac in oldmacs.keys() and mac not in deadmacs:
            macsout[mac] = oldmacs[mac]

        if mac not in macsout.keys():
            logger.error(
                "Error testing MAC({}) for Portal({})".format(mac, new_name)
            )
            if not is_json:
                flash(
                    "Error testing MAC({}) for Portal({})".format(mac, new_name),
                    "danger",
                )

    if len(macsout) > 0:
        portal_entry["enabled"] = enabled
        portal_entry["name"] = new_name
        portal_entry["url"] = url
        portal_entry["macs"] = macsout
        portal_entry["streams per mac"] = streamsPerMac
        portal_entry["epg offset"] = epgOffset
        portal_entry["proxy"] = proxy

        new_id = new_name
        if new_id != old_id:
            portals.pop(old_id, None)
        portals[new_id] = portal_entry

        savePortals(portals)
        logger.info("Portal({}) updated!".format(new_name))
        if is_json:
            return jsonify({"success": True, "portal": {"id": new_id, "name": new_name}}), 200
        flash("Portal({}) updated!".format(new_name), "success")
        return redirect("/portals", code=302)
    else:
        logger.error(
            "None of the MACs tested OK for Portal({}). Updating not successfull".format(
                new_name
            )
        )
        msg = (
            "Keine der MACs konnte erfolgreich getestet werden. "
            "Portal wurde nicht aktualisiert."
        )
        if is_json:
            return jsonify({"success": False, "error": msg}), 400
        flash(msg, "danger")
        return redirect("/portals", code=302)


@app.route("/portal/remove", methods=["POST"])
@authorise
def portalRemove():
    id = request.form["deleteId"]
    portals = getPortals()
    name = portals[id]["name"]
    del portals[id]
    savePortals(portals)
    logger.info("Portal ({}) removed!".format(name))
    flash("Portal ({}) removed!".format(name), "success")
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
        logger.info(f"getting Data from {portal}")
        if portals[portal]["enabled"] == "true":
            portalName = portals[portal]["name"]
            url = portals[portal]["url"]
            macs = list(portals[portal]["macs"].keys())
            proxy = portals[portal]["proxy"]
            enabledChannels = portals[portal].get("enabled channels", [])
            customChannelNames = portals[portal].get("custom channel names", {})
            customGenres = portals[portal].get("custom genres", {})
            customChannelNumbers = portals[portal].get("custom channel numbers", {})
            customEpgIds = portals[portal].get("custom epg ids", {})
            fallbackChannels = portals[portal].get("fallback channels", {})

            for mac in macs:
                logger.info(f"Using mac: {mac}")
                try:
                    token = stb.getToken(url, mac, proxy)
                    stb.getProfile(url, mac, token, proxy)
                    allChannels = stb.getAllChannels(url, mac, token, proxy)
                    genres = stb.getGenreNames(url, mac, token, proxy)
                    break
                except Exception:
                    allChannels = None
                    genres = None

            if allChannels and genres:
                for channel in allChannels:
                    channelId = str(channel["id"])
                    channelName = str(channel["name"])
                    channelNumber = str(channel["number"])
                    genre = str(genres.get(str(channel["tv_genre_id"])))
                    enabled = channelId in enabledChannels
                    customChannelNumber = customChannelNumbers.get(channelId) or ""
                    customChannelName = customChannelNames.get(channelId) or ""
                    customGenre = customGenres.get(channelId) or ""
                    customEpgId = customEpgIds.get(channelId) or ""
                    fallbackChannel = fallbackChannels.get(channelId) or ""
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
    global cached_xmltv, last_playlist_host
    threading.Thread(target=refresh_xmltv, daemon=True).start()
    last_playlist_host = None
    Thread(target=refresh_lineup).start()

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
            if channelId not in portals[portal]["enabled channels"]:
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
            portals[portal]["custom channel numbers"].pop(channelId, None)

    for edit in nameEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        customName = edit["custom name"]
        if customName:
            portals[portal].setdefault("custom channel names", {})
            portals[portal]["custom channel names"].update({channelId: customName})
        else:
            portals[portal]["custom channel names"].pop(channelId, None)

    for edit in genreEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        customGenre = edit["custom genre"]
        if customGenre:
            portals[portal].setdefault("custom genres", {})
            portals[portal]["custom genres"].update({channelId: customGenre})
        else:
            portals[portal]["custom genres"].pop(channelId, None)

    for edit in epgEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        customEpgId = edit["custom epg id"]
        if customEpgId:
            portals[portal].setdefault("custom epg ids", {})
            portals[portal]["custom epg ids"].update({channelId: customEpgId})
        else:
            portals[portal]["custom epg ids"].pop(channelId, None)

    for edit in fallbackEdits:
        portal = edit["portal"]
        channelId = edit["channel id"]
        channelName = edit["channel name"]
        if channelName:
            portals[portal].setdefault("fallback channels", {})
            portals[portal]["fallback channels"].update({channelId: channelName})
        else:
            portals[portal]["fallback channels"].pop(channelId, None)

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
    Thread(target=refresh_xmltv).start()
    flash("Settings saved!", "success")
    return redirect("/settings", code=302)


@app.route("/playlist.m3u", methods=["GET"])
@authorise
def playlist():
    global cached_playlist, last_playlist_host

    logger.info("Playlist Requested")

    current_host = request.host or "0.0.0.0:8001"

    if (
        cached_playlist is None
        or len(cached_playlist) == 0
        or last_playlist_host != current_host
    ):
        logger.info(
            f"Regenerating playlist due to host change: {last_playlist_host} -> {current_host}"
        )
        last_playlist_host = current_host
        generate_playlist()

    return Response(cached_playlist, mimetype="text/plain")


@app.route("/m3u/<portalId>", methods=["GET"])
@authorise
def playlist_portal(portalId):
    """Return a M3U playlist for a specific portal only. portalId == portal name."""
    logger.info(f"Per-portal playlist requested for portalId='{portalId}'")

    portals = getPortals()
    if portalId not in portals:
        logger.warning(f"Requested playlist for unknown portalId: {portalId}")
        return Response("#EXTM3U\n", mimetype="text/plain")

    portal_cfg = portals[portalId]
    if portal_cfg.get("enabled") != "true":
        logger.info(f"Requested playlist for disabled portalId: {portalId}")
        return Response("#EXTM3U\n", mimetype="text/plain")

    enabledChannels = portal_cfg.get("enabled channels", [])
    if not enabledChannels:
        logger.info(f"No enabled channels for portalId: {portalId}")
        return Response("#EXTM3U\n", mimetype="text/plain")

    playlist_host = request.host or "0.0.0.0:8001"

    url = portal_cfg.get("url")
    proxy = portal_cfg.get("proxy")
    macs = list(portal_cfg.get("macs", {}).keys())
    customChannelNames = portal_cfg.get("custom channel names", {})
    customGenres = portal_cfg.get("custom genres", {})
    customChannelNumbers = portal_cfg.get("custom channel numbers", {})
    customEpgIds = portal_cfg.get("custom epg ids", {})

    allChannels = None
    genres = None

    for mac in macs:
        try:
            token = stb.getToken(url, mac, proxy)
            stb.getProfile(url, mac, token, proxy)
            allChannels = stb.getAllChannels(url, mac, token, proxy)
            genres = stb.getGenreNames(url, mac, token, proxy)
            break
        except Exception as e:
            logger.warning(f"Failed to init portal {portalId} with MAC {mac}: {e}")
            allChannels = None
            genres = None

    channels = []
    if allChannels and genres:
        for channel in allChannels:
            channelId = str(channel.get("id"))
            if channelId not in enabledChannels:
                continue

            channelName = customChannelNames.get(channelId) or str(channel.get("name"))
            genreId = str(channel.get("tv_genre_id"))
            genre = customGenres.get(channelId) or str(genres.get(genreId))
            channelNumber = (
                customChannelNumbers.get(channelId) or str(channel.get("number"))
            )
            epgId = customEpgIds.get(channelId) or channelName

            line = '#EXTINF:-1 tvg-id="' + epgId + '"'
            if getSettings().get("use channel numbers", "true") == "true":
                line += f' tvg-chno="{channelNumber}"'
            if getSettings().get("use channel genres", "true") == "true":
                line += f' group-title="{genre}"'
            line += ',"' + channelName + '"\n'
            line += f"http://{playlist_host}/play/{portalId}/{channelId}"
            channels.append(line)

    if getSettings().get("sort playlist by channel name", "true") == "true":
        channels.sort(key=lambda k: k.split(",")[1].split("\n")[0])
    if getSettings().get("use channel numbers", "true") == "true":
        if getSettings().get("sort playlist by channel number", "false") == "true":
            channels.sort(key=lambda k: k.split('tvg-chno="')[1].split('"')[0])
    if getSettings().get("use channel genres", "true") == "true":
        if getSettings().get("sort playlist by channel genre", "false") == "true":
            channels.sort(key=lambda k: k.split('group-title="')[1].split('"')[0])

    playlist_str = "#EXTM3U \n" + "\n".join(channels)
    return Response(playlist_str, mimetype="text/plain")


@app.route("/update_playlistm3u", methods=["POST"])
def update_playlistm3u():
    generate_playlist()
    return Response("Playlist updated successfully", status=200)


def generate_playlist():
    global cached_playlist
    logger.info("Generating playlist.m3u...")

    playlist_host = request.host or "0.0.0.0:8001"

    channels = []
    portals = getPortals()

    for portal in portals:
        if portals[portal]["enabled"] == "true":
            enabledChannels = portals[portal].get("enabled channels", [])
            if len(enabledChannels) != 0:
                name = portals[portal]["name"]
                url = portals[portal]["url"]
                macs = list(portals[portal]["macs"].keys())
                proxy = portals[portal]["proxy"]
                customChannelNames = portals[portal].get("custom channel names", {})
                customGenres = portals[portal].get("custom genres", {})
                customChannelNumbers = portals[portal].get("custom channel numbers", {})
                customEpgIds = portals[portal].get("custom epg ids", {})

                for mac in macs:
                    try:
                        token = stb.getToken(url, mac, proxy)
                        stb.getProfile(url, mac, token, proxy)
                        allChannels = stb.getAllChannels(url, mac, token, proxy)
                        genres = stb.getGenreNames(url, mac, token, proxy)
                        break
                    except Exception:
                        allChannels = None
                        genres = None

                if allChannels and genres:
                    for channel in allChannels:
                        channelId = str(channel.get("id"))
                        if channelId in enabledChannels:
                            channelName = customChannelNames.get(channelId)
                            if channelName is None:
                                channelName = str(channel.get("name"))
                            genre = customGenres.get(channelId)
                            if genre is None:
                                genreId = str(channel.get("tv_genre_id"))
                                genre = str(genres.get(genreId))
                            channelNumber = customChannelNumbers.get(channelId)
                            if channelNumber is None:
                                channelNumber = str(channel.get("number"))
                            epgId = customEpgIds.get(channelId)
                            if epgId is None:
                                epgId = channelName
                            channels.append(
                                "#EXTINF:-1"
                                + ' tvg-id="'
                                + epgId
                                + (
                                    '" tvg-chno="' + channelNumber
                                    if getSettings().get(
                                        "use channel numbers", "true"
                                    )
                                    == "true"
                                    else ""
                                )
                                + (
                                    '" group-title="' + genre
                                    if getSettings().get(
                                        "use channel genres", "true"
                                    )
                                    == "true"
                                    else ""
                                )
                                + '",'
                                + channelName
                                + "\n"
                                + "http://"
                                + playlist_host
                                + "/play/"
                                + portal
                                + "/"
                                + channelId
                            )
                else:
                    logger.error("Error making playlist for {}, skipping".format(name))

    if getSettings().get("sort playlist by channel name", "true") == "true":
        channels.sort(key=lambda k: k.split(",")[1].split("\n")[0])
    if getSettings().get("use channel numbers", "true") == "true":
        if getSettings().get("sort playlist by channel number", "false") == "true":
            channels.sort(key=lambda k: k.split('tvg-chno="')[1].split('"')[0])
    if getSettings().get("use channel genres", "true") == "true":
        if getSettings().get("sort playlist by channel genre", "false") == "true":
            channels.sort(key=lambda k: k.split('group-title="')[1].split('"')[0])

    playlist = "#EXTM3U \n" + "\n".join(channels)
    cached_playlist = playlist
    logger.info("Playlist generated and cached.")


def refresh_xmltv():
    settings = getSettings()
    logger.info("Refreshing XMLTV...")

    cache_dir = "/app/data"
    os.makedirs(cache_dir, exist_ok=True)
    cache_file = os.path.join(cache_dir, "MacReplayV2EPG.xml")
    legacy_cache_file = os.path.join(cache_dir, "MacReplayEPG.xml")
    if not os.path.exists(cache_file) and os.path.exists(legacy_cache_file):
        shutil.copy2(legacy_cache_file, cache_file)
        logger.info(
            "Legacy MacReplay EPG cache detected – migrated to MacReplayV2EPG.xml"
        )

    day_before_yesterday = datetime.now(timezone.utc) - timedelta(days=2)
    day_before_yesterday_str = (
        day_before_yesterday.strftime("%Y%m%d%H%M%S") + " +0000"
    )

    cached_programmes = []
    if os.path.exists(cache_file):
        try:
            tree = ET.parse(cache_file)
            root = tree.getroot()
            for programme in root.findall("programme"):
                stop_attr = programme.get("stop")
                if stop_attr:
                    try:
                        stop_time = datetime.strptime(
                            stop_attr.split(" ")[0], "%Y%m%d%H%M%S"
                        ).replace(tzinfo=timezone.utc)
                        if stop_time >= day_before_yesterday:
                            cached_programmes.append(
                                ET.tostring(programme, encoding="unicode")
                            )
                    except ValueError as e:
                        logger.warning(
                            f"Invalid stop time format in cached programme: {stop_attr}. Skipping."
                        )
            logger.info("Loaded existing programme data from cache.")
        except Exception as e:
            logger.error(f"Failed to load cache file: {e}")

    channels = ET.Element("tv")
    programmes = ET.Element("tv")
    portals = getPortals()

    for portal in portals:
        if portals[portal]["enabled"] == "true":
            portal_name = portals[portal]["name"]
            portal_epg_offset = int(portals[portal]["epg offset"])
            logger.info(
                f"Fetching EPG | Portal: {portal_name} | offset: {portal_epg_offset} |"
            )

            enabledChannels = portals[portal].get("enabled channels", [])
            if len(enabledChannels) != 0:
                name = portals[portal]["name"]
                url = portals[portal]["url"]
                macs = list(portals[portal]["macs"].keys())
                proxy = portals[portal]["proxy"]
                customChannelNames = portals[portal].get("custom channel names", {})
                customEpgIds = portals[portal].get("custom epg ids", {})
                customChannelNumbers = portals[portal].get(
                    "custom channel numbers", {}
                )

                for mac in macs:
                    try:
                        token = stb.getToken(url, mac, proxy)
                        stb.getProfile(url, mac, token, proxy)
                        allChannels = stb.getAllChannels(url, mac, token, proxy)
                        epg = stb.getEpg(url, mac, token, 24, proxy)
                        break
                    except Exception as e:
                        allChannels = None
                        epg = None
                        logger.error(f"Error fetching data for MAC {mac}: {e}")

                if allChannels and epg:
                    for channel in allChannels:
                        try:
                            channelId = str(channel.get("id"))
                            if str(channelId) in enabledChannels:
                                channelName = customChannelNames.get(
                                    channelId, channel.get("name")
                                )
                                channelNumber = customChannelNumbers.get(
                                    channelId, str(channel.get("number"))
                                )
                                epgId = customEpgIds.get(channelId, channelNumber)

                                channelEle = ET.SubElement(
                                    channels, "channel", id=epgId
                                )
                                ET.SubElement(
                                    channelEle, "display-name"
                                ).text = channelName
                                ET.SubElement(
                                    channelEle, "icon", src=channel.get("logo")
                                )

                                if channelId not in epg or not epg.get(channelId):
                                    logger.warning(
                                        f"No EPG data found for channel {channelName} (ID: {channelId}), Creating a Dummy EPG item."
                                    )
                                    start_time = datetime.now(timezone.utc).replace(
                                        minute=0, second=0, microsecond=0
                                    )
                                    stop_time = start_time + timedelta(hours=24)
                                    start = (
                                        start_time.strftime("%Y%m%d%H%M%S")
                                        + " +0000"
                                    )
                                    stop = (
                                        stop_time.strftime("%Y%m%d%H%M%S")
                                        + " +0000"
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
                                    ).text = channelName
                                    ET.SubElement(
                                        programmeEle, "desc"
                                    ).text = channelName
                                else:
                                    for p in epg.get(channelId):
                                        try:
                                            start_time = datetime.fromtimestamp(
                                                p.get("start_timestamp"),
                                                timezone.utc,
                                            ) + timedelta(
                                                hours=portal_epg_offset
                                            )
                                            stop_time = datetime.fromtimestamp(
                                                p.get("stop_timestamp"),
                                                timezone.utc,
                                            ) + timedelta(
                                                hours=portal_epg_offset
                                            )
                                            start = (
                                                start_time.strftime("%Y%m%d%H%M%S")
                                                + " +0000"
                                            )
                                            stop = (
                                                stop_time.strftime("%Y%m%d%H%M%S")
                                                + " +0000"
                                            )
                                            if start <= day_before_yesterday_str:
                                                continue
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
                                        except Exception as e:
                                            logger.error(
                                                f"Error processing programme for channel {channelName} (ID: {channelId}): {e}"
                                            )
                                            pass
                        except Exception as e:
                            logger.error(
                                f"| Channel:{channelNumber} | {channelName} | {e}"
                            )
                            pass
                else:
                    logger.error(f"Error making XMLTV for {name}, skipping")

    xmltv = channels
    for programme in programmes.iter("programme"):
        xmltv.append(programme)

    existing_programme_hashes = {
        ET.tostring(p, encoding="unicode")
        for p in xmltv.findall("programme")
    }
    for cached in cached_programmes:
        if cached not in existing_programme_hashes:
            xmltv.append(ET.fromstring(cached))

    rough_string = ET.tostring(xmltv, encoding="unicode")
    reparsed = minidom.parseString(rough_string)
    formatted_xmltv = "\n".join(
        [line for line in reparsed.toprettyxml(indent="  ").splitlines() if line.strip()]
    )

    with open(cache_file, "w", encoding="utf-8") as f:
        f.write(formatted_xmltv)
    logger.info("XMLTV cache updated.")

    global cached_xmltv, last_updated
    cached_xmltv = formatted_xmltv
    last_updated = time.time()
    logger.debug(f"Generated XMLTV: {formatted_xmltv}")


@app.route("/xmltv", methods=["GET"])
@authorise
def xmltv():
    global cached_xmltv, last_updated
    logger.info("Guide Requested")

    if cached_xmltv is None or (time.time() - last_updated) > 900:
        refresh_xmltv()

    return Response(
        cached_xmltv,
        mimetype="text/xml",
    )


@app.route("/play/<portalId>/<channelId>", methods=["GET"])
def channel(portalId, channelId):
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
            try:
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
            except Exception:
                pass

        try:
            startTime = datetime.now(timezone.utc).timestamp()
            occupy()
            with subprocess.Popen(
                ffmpegcmd,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
            ) as ffmpeg_sp:
                while True:
                    chunk = ffmpeg_sp.stdout.read(1024)
                    if len(chunk) == 0:
                        if ffmpeg_sp.poll() != 0:
                            logger.info(
                                "Ffmpeg closed with error({}). Moving MAC({}) for Portal({})".format(
                                    str(ffmpeg_sp.poll()), mac, portalName
                                )
                            )
                            moveMac(portalId, mac)
                        break
                    yield chunk
        except Exception:
            pass
        finally:
            try:
                unoccupy()
            except Exception:
                pass
            try:
                ffmpeg_sp.kill()
            except Exception:
                pass

    def testStream():
        timeout = int(getSettings()["ffmpeg timeout"]) * int(1000000)
        ffprobecmd = [ffprobe_path, "-timeout", str(timeout), "-i", link]

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

    def isMacFree():
        count = 0
        for i in occupied.get(portalId, []):
            if i["mac"] == mac:
                count = count + 1
        if count < streamsPerMac:
            return True
        else:
            return False

    portal = getPortals().get(portalId)
    if not portal:
        return make_response("Portal not found", 404)

    portalName = portal.get("name")
    url = portal.get("url")
    macs = list(portal["macs"].keys())
    streamsPerMac = int(portal.get("streams per mac"))
    proxy = portal.get("proxy")
    web = request.args.get("web")
    ip = request.remote_addr

    logger.info(
        "IP({}) requested Portal({}):Channel({})".format(ip, portalId, channelId)
    )

    freeMac = False

    for mac in macs:
        channels = None
        cmd = None
        link = None
        if streamsPerMac == 0 or isMacFree():
            logger.info(
                "Trying Portal({}):MAC({}):Channel({})".format(
                    portalId, mac, channelId
                )
            )
            freeMac = True
            token = stb.getToken(url, mac, proxy)
            if token:
                stb.getProfile(url, mac, token, proxy)
                channels = stb.getAllChannels(url, mac, token, proxy)

        if channels:
            for c in channels:
                if str(c["id"]) == channelId:
                    channelName = portal.get("custom channel names", {}).get(
                        channelId
                    )
                    if channelName is None:
                        channelName = c["name"]
                    cmd = c["cmd"]
                    break

        if cmd:
            if "http://localhost/" in cmd:
                link = stb.getLink(url, mac, token, cmd, proxy)
            else:
                parts = cmd.split(" ")
                if len(parts) > 1:
                    link = parts[1]
                else:
                    link = cmd

        if link:
            if (
                getSettings().get("test streams", "true") == "false"
                or testStream()
            ):
                if web:
                    ffmpegcmd = [
                        ffmpeg_path,
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
                    return Response(
                        streamData(), mimetype="application/octet-stream"
                    )

                else:
                    if (
                        getSettings().get("stream method", "ffmpeg")
                        == "ffmpeg"
                    ):
                        ffmpegcmd_str = (
                            f"{ffmpeg_path} {getSettings()['ffmpeg command']}"
                        )
                        ffmpegcmd_str = ffmpegcmd_str.replace("<url>", link)
                        ffmpegcmd_str = ffmpegcmd_str.replace(
                            "<timeout>",
                            str(
                                int(getSettings()["ffmpeg timeout"])
                                * int(1000000)
                            ),
                        )
                        if proxy:
                            ffmpegcmd_str = ffmpegcmd_str.replace(
                                "<proxy>", proxy
                            )
                        else:
                            ffmpegcmd_str = ffmpegcmd_str.replace(
                                "-http_proxy <proxy>", ""
                            )
                        ffmpegcmd_str = " ".join(ffmpegcmd_str.split())
                        ffmpegcmd = ffmpegcmd_str.split()
                        return Response(
                            streamData(), mimetype="application/octet-stream"
                        )
                    else:
                        logger.info("Redirect sent")
                        return redirect(link)

        logger.info(
            "Unable to connect to Portal({}) using MAC({})".format(portalId, mac)
        )
        logger.info("Moving MAC({}) for Portal({})".format(mac, portalName))
        moveMac(portalId, mac)

        if not getSettings().get("try all macs", "true") == "true":
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
    logFilePath = "/app/logs/MacReplayV2.log"

    try:
        with open(logFilePath) as f:
            log_content = f.read()
        return log_content
    except FileNotFoundError:
        return "Log file not found"


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
    logger.info("HDHR Status Requested.")
    settings = getSettings()
    name = settings["hdhr name"]
    id = settings["hdhr id"]
    tuners = settings["hdhr tuners"]
    data = {
        "BaseURL": host,
        "DeviceAuth": name,
        "DeviceID": id,
        "FirmwareName": "MacReplayV2",
        "FirmwareVersion": "666",
        "FriendlyName": name,
        "LineupURL": host + "/lineup.json",
        "Manufacturer": "Evilvirus",
        "ModelNumber": "666",
        "TunerCount": int(tuners),
    }
    return flask.jsonify(data)


@app.route("/lineup_status.json", methods=["GET"])
@hdhr
def status():
    data = {
        "ScanInProgress": 0,
        "ScanPossible": 0,
        "Source": "Cable",
        "SourceList": ["Cable"],
    }
    return flask.jsonify(data)


def refresh_lineup():
    global cached_lineup
    logger.info("Refreshing Lineup...")
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
                customChannelNames = portals[portal].get(
                    "custom channel names", {}
                )
                customChannelNumbers = portals[portal].get(
                    "custom channel numbers", {}
                )

                for mac in macs:
                    try:
                        token = stb.getToken(url, mac, proxy)
                        stb.getProfile(url, mac, token, proxy)
                        allChannels = stb.getAllChannels(url, mac, token, proxy)
                        break
                    except Exception:
                        allChannels = None

                if allChannels:
                    for channel in allChannels:
                        channelId = str(channel.get("id"))
                        if channelId in enabledChannels:
                            channelName = customChannelNames.get(channelId)
                            if channelName is None:
                                channelName = str(channel.get("name"))
                            channelNumber = customChannelNumbers.get(channelId)
                            if channelNumber is None:
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
                    logger.error(
                        "Error making lineup for {}, skipping".format(name)
                    )

    lineup.sort(key=lambda x: int(x["GuideNumber"]))

    cached_lineup = lineup
    logger.info("Lineup Refreshed.")


@app.route("/lineup.json", methods=["GET"])
@app.route("/lineup.post", methods=["POST"])
@hdhr
def lineup():
    logger.info("Lineup Requested")
    if not cached_lineup:
        refresh_lineup()
    logger.info("Lineup Delivered")
    return jsonify(cached_lineup)


@app.route("/refresh_lineup", methods=["POST"])
def refresh_lineup_endpoint():
    refresh_lineup()
    return jsonify({"status": "Lineup refreshed successfully"})


def start_refresh():
    threading.Thread(target=refresh_lineup, daemon=True).start()
    start_epg_scheduler()


def start_epg_scheduler(interval_seconds: int = EPG_REFRESH_INTERVAL_SECONDS):
    interval_hours = interval_seconds / 3600

    def _epg_worker():
        logger.info(
            f"Background EPG refresh thread started; updating every {interval_hours:.2f} hour(s)."
        )
        while True:
            try:
                refresh_xmltv()
                logger.info("Background EPG refresh completed.")
            except Exception as exc:
                logger.error(f"Background EPG refresh failed: {exc}")
            time.sleep(interval_seconds)

    threading.Thread(
        target=_epg_worker, daemon=True, name="EPGRefreshScheduler"
    ).start()


if __name__ == "__main__":
    config = loadConfig()
    start_refresh()

    # Always use waitress for production in container
    waitress.serve(app, host="0.0.0.0", port=8001, _quiet=True, threads=24)
