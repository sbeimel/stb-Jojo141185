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
fileHandler = logging.FileHandler("proxy.log")
fileHandler.setFormatter(logFormat)
logger = logging.getLogger("STB-Proxy")

logger.addHandler(fileHandler)
logging.basicConfig(level=logging.INFO)

config_file_name = "config.json"
debugMode = False


# Function to check and create the config file if it doesn't exist
def check_and_create_config():
    if not os.path.exists(config_file_name):
        logger.warning("No existing config found. Creating a new one")
        default_config = {
            "portals": {},
            "settings": {
                "version": "1.0.0",
                "language": "en",
                "proxy": None,
                "update_url": "",
                "username": "",
                "password": ""
            }
        }
        saveConfig(default_config)

# Function to load the configuration from the file
def loadConfig():
    check_and_create_config()  # Ensure config file exists
    with open(config_file_name, "r") as f:
        config = json.load(f)
    return config

# Function to save the configuration to the file
def saveConfig(config):
    with open(config_file_name, "w") as f:
        json.dump(config, f, indent=4)

def getSettings():
    config = loadConfig()
    return config.get("settings", {})

def saveSettings(settings):
    config = loadConfig()
    config["settings"] = settings
    saveConfig(config)

def getPortals():
    config = loadConfig()
    return config.get("portals", {})

def savePortals(portals):
    config = loadConfig()
    config["portals"] = portals
    saveConfig(config)

def settings():
    settings = getSettings()

    return render_template("settings.html", settings=settings)


def saveSettingsForm(settings_dict):
    settings = getSettings()
    settings.update(settings_dict)
    saveSettings(settings)

def get_proxy():
    settings = getSettings()
    return settings.get("proxy", None)

def authorise(f):
    @wraps(f)
    def decorated_function(*args, **kwargs):
        settings = getSettings()
        username = settings.get("username")
        password = settings.get("password")

        if username and password:
            auth = request.authorization

            if not auth or not (auth.username == username and auth.password == password):
                return Response(
                    "Login required.",
                    401,
                    {"WWW-Authenticate": 'Basic realm="Login Required"'},
                )
        return f(*args, **kwargs)
    return decorated_function


@app.route("/")
@authorise
def home():
    portals = getPortals()
    settings_data = getSettings()
    return render_template("index.html", portals=portals, settings=settings_data)


@app.route("/settings", methods=["GET", "POST"])
@authorise
def settings_page():
    if request.method == "POST":
        settings_dict = {
            "proxy": request.form.get("proxy"),
            "update_url": request.form.get("update_url"),
            "username": request.form.get("username"),
            "password": request.form.get("password"),
        }
        saveSettingsForm(settings_dict)
        flash("Settings updated successfully", "success")
        return redirect("/settings")

    current_settings = getSettings()
    return render_template("settings.html", settings=current_settings)


default_mac_info = {"expiry": None, "stats": {"playtime": 0, "errors": 0, "requests": 0}}

defaultPortal = {
    "enabled": "true",
    "name": "",
    "url": "",
    "macs": defaultdict(lambda: copy.deepcopy(default_mac_info)),
    "streams per mac": "1",
    "epgTimeOffset": "0",
    "proxy": "",
    "enabled channels": [],
    "custom channel names": {},
    "custom channel numbers": {},
    "custom genres": {},
    "custom epg ids": {},
    "catchup type": "stalker",
    "catchup days": "7",
    "custom cmd": "",
    "user agent": "",
    "timeout": "10",
    "buffer size": "1024",
    "max connections": "3",
}

def test_mac_addresses(url, portal_proxy, macs_to_test, portal_name, time_zone):
    valid_macs = []
    dead_macs = []

    logger.info(
        f"Testing MAC addresses for Portal({portal_name}) with URL({url}) and proxy({portal_proxy})"
    )

    for mac in macs_to_test:
        try:
            logger.info(f"Testing MAC({mac}) for Portal({portal_name})")
            result = stb.getProfile(url, mac, portal_proxy, time_zone)

            if result and "success" in result:
                expiry = result.get("expiry", None)
                if expiry:
                    expiry_date = parse(expiry).replace(tzinfo=timezone.utc)
                else:
                    expiry_date = None

                valid_macs.append({"mac": mac, "expiry": expiry_date})
            else:
                logger.info(f"MAC({mac}) for Portal({portal_name}) is dead")
                dead_macs.append(mac)

        except Exception as e:
            logger.error(f"Error testing MAC({mac}) for Portal({portal_name}): {e}")
            dead_macs.append(mac)

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

    return portal


@app.route("/portal/add", methods=["POST"])
@authorise
def portals_add():
    """
    Adds a new portal configuration.
    Accepts both classic HTML form posts and JSON (AJAX) requests.
    """
    # Accept both JSON (AJAX) and classic form submits
    if request.is_json:
        data = request.get_json(silent=True) or {}
        name = data.get("name")
        url = data.get("url")
        proxy = data.get("proxy")
        streams_per_mac = data.get("streams per mac") or data.get("streams_per_mac")
        epg_time_offset = data.get("epg time offset") or data.get("epgTimeOffset")
        time_zone = data.get("time_zone")
        macs = data.get("macs") or []
    else:
        name = request.form.get("name")
        url = request.form.get("url")
        proxy = request.form.get("proxy")
        streams_per_mac = request.form.get("streams per mac")
        epg_time_offset = request.form.get("epg time offset")
        time_zone = request.form.get("time_zone")

        # Try multiple possible field names for MACs in the HTML form
        macs_data = (
            request.form.get("macs")
            or request.form.get("macs[]")
            or request.form.get("mac")
            or request.form.get("mac_addresses")
            or ""
        )

        macs = []

        # 1) Try to interpret macs_data as JSON (e.g. ["AA:BB:CC:DD:EE:FF"])
        try:
            parsed = json.loads(macs_data)
            if isinstance(parsed, list):
                macs = parsed
            elif isinstance(parsed, str):
                macs_data = parsed
        except Exception:
            # Not JSON, fall back to plain text parsing below
            pass

        # 2) If still empty, treat as plain text (textarea):
        #    allow separators like newlines, commas, semicolons
        if not macs and isinstance(macs_data, str):
            tmp = macs_data.replace("\r", "\n")
            for sep in [",", ";"]:
                tmp = tmp.replace(sep, "\n")
            macs = [m.strip() for m in tmp.split("\n") if m.strip()]

    logger.info(f"Add portal request: name={name}, url={url}, macs={macs}")

    # Normalize MACs list
    if isinstance(macs, str):
        tmp = macs.replace("\r", "\n")
        for sep in [",", ";"]:
            tmp = tmp.replace(sep, "\n")
        macs = [m.strip() for m in tmp.split("\n") if m.strip()]

    macs = [m.strip().upper() for m in macs if isinstance(m, str) and m.strip()]

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
        # Initialize MACs as a dict with default stats structure
        "macs": defaultdict(lambda: copy.deepcopy(default_mac_info)),
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
        macs = data.get("macs", [])
    else:
        # Handling the regular form submission
        id = request.form.get("id")
        name = request.form.get("name")
        url = request.form.get("url")
        proxy = request.form.get("proxy")
        time_zone = request.form.get("time_zone")
        macs = request.form.get("macs", "").split(",")

    logger.info(f"Check MACs request for portal ID={id}, name={name}, URL={url}, MACs={macs}")

    portals = getPortals()

    if id == "new":
        portal = {
            "enabled": "true",
            "name": name,
            "url": url,
            "macs": defaultdict(lambda: copy.deepcopy(default_mac_info)),
            "streams per mac": "1",
            "epgTimeOffset": "0",
            "time_zone": time_zone,
            "proxy": proxy,
        }
    else:
        portal = portals.get(id)
        if not portal:
            logger.error(f"No portal found with ID({id})")
            return jsonify({"error": f"No portal found with ID({id})"}), 404

        portal["name"] = name
        portal["url"] = url
        portal["proxy"] = proxy
        portal["time_zone"] = time_zone

    # Update MAC addresses in the portal
    portal = portal_update_macs(portal, macs=macs if id == "new" else None, retest=id != "new")

    # Return updated portal data as JSON
    return jsonify({"success": True, "portal": portal}), 200


@app.route("/portal/<id>/delete", methods=["POST"])
@authorise
def portal_delete(id):
    portals = getPortals()
    if id in portals:
        del portals[id]
        savePortals(portals)
        flash("Portal deleted successfully", "success")
    else:
        flash("Portal not found", "danger")
    return redirect("/")


@app.route("/portal/<id>/enable", methods=["POST"])
@authorise
def portal_enable(id):
    portals = getPortals()
    if id in portals:
        portals[id]["enabled"] = "true"
        savePortals(portals)
        flash("Portal enabled successfully", "success")
    else:
        flash("Portal not found", "danger")
    return redirect("/")


@app.route("/portal/<id>/disable", methods=["POST"])
@authorise
def portal_disable(id):
    portals = getPortals()
    if id in portals:
        portals[id]["enabled"] = "false"
        savePortals(portals)
        flash("Portal disabled successfully", "success")
    else:
        flash("Portal not found", "danger")
    return redirect("/")


@app.route("/portal/<id>/edit", methods=["GET", "POST"])
@authorise
def portal_edit(id):
    portals = getPortals()
    portal = portals.get(id)

    if not portal:
        flash("Portal not found", "danger")
        return redirect("/")

    if request.method == "POST":
        portal["name"] = request.form.get("name")
        portal["url"] = request.form.get("url")
        portal["proxy"] = request.form.get("proxy")
        portal["time_zone"] = request.form.get("time_zone")
        savePortals(portals)
        flash("Portal updated successfully", "success")
        return redirect("/")

    return render_template("portal_edit.html", id=id, portal=portal)


@app.route("/portal/<id>/stats", methods=["GET"])
@authorise
def portal_stats(id):
    portals = getPortals()
    portal = portals.get(id)
    if not portal:
        flash("Portal not found", "danger")
        return redirect("/")

    macs = portal.get("macs", {})
    return render_template("portal_stats.html", id=id, portal=portal, macs=macs)


@app.route("/portal/<id>/macs/<mac>/reset", methods=["POST"])
@authorise
def reset_mac_stats(id, mac):
    portals = getPortals()
    portal = portals.get(id)

    if not portal:
        flash("Portal not found", "danger")
        return redirect("/")

    if mac in portal["macs"]:
        portal["macs"][mac]["stats"] = {"playtime": 0, "errors": 0, "requests": 0}
        savePortals(portals)
        flash(f"Stats for MAC({mac}) reset successfully", "success")
    else:
        flash("MAC address not found", "danger")

    return redirect(url_for("portal_stats", id=id))


@app.route("/portal/<id>/macs/<mac>/delete", methods=["POST"])
@authorise
def delete_mac(id, mac):
    portals = getPortals()
    portal = portals.get(id)

    if not portal:
        flash("Portal not found", "danger")
        return redirect("/")

    if mac in portal["macs"]:
        del portal["macs"][mac]
        savePortals(portals)
        flash(f"MAC({mac}) deleted successfully", "success")
    else:
        flash("MAC address not found", "danger")

    return redirect(url_for("portal_stats", id=id))


@app.route("/portal/<id>/macs/<mac>/update_expiry", methods=["POST"])
@authorise
def update_mac_expiry(id, mac):
    portals = getPortals()
    portal = portals.get(id)

    if not portal:
        flash("Portal not found", "danger")
        return redirect("/")

    new_expiry = request.form.get("expiry")

    if mac in portal["macs"]:
        try:
            new_expiry_date = parse(new_expiry).replace(tzinfo=timezone.utc)
            portal["macs"][mac]["expiry"] = new_expiry_date
            savePortals(portals)
            flash(f"Expiry date for MAC({mac}) updated successfully", "success")
        except Exception as e:
            flash(f"Invalid expiry date format: {e}", "danger")
    else:
        flash("MAC address not found", "danger")

    return redirect(url_for("portal_stats", id=id))


@app.route("/update_portal", methods=["POST"])
@authorise
def update_portal():
    data = request.get_json()
    id = data.get("id")
    new_data = data.get("portal")
    portals = getPortals()

    if id in portals:
        portals[id].update(new_data)
        savePortals(portals)
        return jsonify({"success": True}), 200
    else:
        return jsonify({"error": "Portal not found"}), 404

# Proxy and streaming routes (unchanged logic)...

@app.route("/play/<portal_id>/<mac>/<cmd>/<int:channel_id>")
def play_channel(portal_id, mac, cmd, channel_id):
    portals = getPortals()
    portal = portals.get(portal_id)

    if not portal:
        return "Portal not found", 404

    mac_info = portal["macs"].get(mac.upper())
    if not mac_info:
        return "MAC address not found", 404

    url = portal["url"]
    proxy = portal.get("proxy")
    time_zone = portal.get("time_zone")
    user_agent = portal.get("user agent", "")
    timeout = int(portal.get("timeout", "10"))
    bufferSize = int(portal.get("buffer size", "1024"))

    # Update stats
    mac_info["stats"]["requests"] += 1
    savePortals(portals)

    def generate():
        with lock:
            try:
                logger.info(f"Starting stream for Portal({portal['name']}), MAC({mac}), Channel({channel_id})")
                with stb.getStream(
                    url, mac, proxy, cmd, channel_id, time_zone, user_agent, timeout
                ) as stream:
                    ffmpeg_path = "ffmpeg"
                    ffmpeg_cmd = [
                        ffmpeg_path,
                        "-i",
                        "pipe:0",
                        "-c",
                        "copy",
                        "-f",
                        "mpegts",
                        "pipe:1",
                    ]

                    with subprocess.Popen(
                        ffmpeg_cmd,
                        stdin=subprocess.PIPE,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                    ) as ffmpeg_sp:
                        # Starting stderr read in a separate thread
                        last_stderr = []

                        def read_stderr(proc, buffer):
                            for line in iter(proc.stderr.readline, b""):
                                buffer.append(line.decode(errors="ignore"))
                                if len(buffer) > 100:
                                    buffer.pop(0)

                        stderr_thread = threading.Thread(target=read_stderr, args=(ffmpeg_sp, last_stderr))
                        stderr_thread.start()

                        start_time = time.time()
                        chunk_count = 0

                        while True:
                            chunk = stream.read(bufferSize)
                            if not chunk:
                                logger.info("No streaming data from source detected.")
                                break

                            ffmpeg_sp.stdin.write(chunk)
                            ffmpeg_sp.stdin.flush()
                            out_chunk = ffmpeg_sp.stdout.read(bufferSize)
                            if not out_chunk:
                                logger.info("No streaming data from ffmpeg detected.")
                                if ffmpeg_sp.poll() is not None:
                                    logger.debug("Ffmpeg process closed / exited already with return / error code ({}).".format(str(ffmpeg_sp.poll())))
                                    error_text = "\n".join(last_stderr)
                                    logger.error(f"FFmpeg error: {error_text}")
                                    break
                            else:
                                yield out_chunk

                            chunk_count += 1

                        duration = time.time() - start_time
                        mac_info["stats"]["playtime"] += duration
                        savePortals(portals)

            except Exception as e:
                mac_info["stats"]["errors"] += 1
                savePortals(portals)
                logger.error(f"Error during streaming: {e}")
                return

    return Response(stream_with_context(generate()), mimetype="video/mp2t")


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
