import requests
from requests.adapters import HTTPAdapter, Retry
from urllib.parse import urlparse
import re

s = requests.Session()
retries = Retry(total=3, backoff_factor=0.1, status_forcelist=[500, 502, 503, 504])
s.mount("http://", HTTPAdapter(max_retries=retries))


def getUrl(url, proxy=None):
    def parseResponse(url, data):
        java = data.text.replace(" ", "").replace("'", "").replace("+", "")
        pattern = re.search(r"varpattern.*\/(\(http.*)\/;", java).group(1)
        result = re.search(pattern, url)
        protocolIndex = re.search(r"this\.portal_protocol.*(\d).*;", java).group(1)
        ipIndex = re.search(r"this\.portal_ip.*(\d).*;", java).group(1)
        pathIndex = re.search(r"this\.portal_path.*(\d).*;", java).group(1)
        protocol = result.group(int(protocolIndex))
        ip = result.group(int(ipIndex))
        path = result.group(int(pathIndex))
        portalPatern = re.search(r"this\.ajax_loader=(.*\.php);", java).group(1)
        portal = (
            portalPatern.replace("this.portal_protocol", protocol)
            .replace("this.portal_ip", ip)
            .replace("this.portal_path", path)
        )
        return portal

    url = urlparse(url).scheme + "://" + urlparse(url).netloc
    urls = [
        "/c/xpcom.common.js",
        "/client/xpcom.common.js",
        "/c_/xpcom.common.js",
        "/stalker_portal/c/xpcom.common.js",
        "/stalker_portal/c_/xpcom.common.js",
    ]

    proxies = {"http": proxy, "https": proxy}
    headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)"}

    try:
        for i in urls:
            try:
                response = s.get(url + i, headers=headers, proxies=proxies)
            except:
                response = None
            if response:
                return parseResponse(url + i, response)
    except:
        pass

    # sometimes these pages dont like proxies!
    try:
        for i in urls:
            try:
                response = s.get(url + i, headers=headers)
            except:
                response = None
            if response:
                return parseResponse(url + i, response)
    except:
        pass


def getToken(url, mac, proxy=None, t_zone=None):
    proxies = {"http": proxy, "https": proxy}
    cookies = {"mac": mac, "stb_lang": "en", "timezone": t_zone}
    headers = {"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)"}
    try:
        response = s.get(
            url + "?type=stb&action=handshake&JsHttpRequest=1-xml",
            cookies=cookies,
            headers=headers,
            proxies=proxies,
        )
        token = response.json()["js"]["token"]
        if token:
            return token
    except:
        pass


def getProfile(url, mac, token, proxy=None, t_zone=None):
    proxies = {"http": proxy, "https": proxy}
    cookies = {"mac": mac, "stb_lang": "en", "timezone": t_zone}
    headers = {
        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
        "Authorization": "Bearer " + token,
    }
    try:
        response = s.get(
            url + "?type=stb&action=get_profile&JsHttpRequest=1-xml",
            cookies=cookies,
            headers=headers,
            proxies=proxies,
        )
        profile = response.json()["js"]
        if profile:
            return profile
    except:
        pass


def getExpires(url, mac, token, proxy=None, t_zone=None):
    proxies = {"http": proxy, "https": proxy}
    cookies = {"mac": mac, "stb_lang": "en", "timezone": t_zone}
    headers = {
        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
        "Authorization": "Bearer " + token,
    }
    try:
        response = s.get(
            url + "?type=account_info&action=get_main_info&JsHttpRequest=1-xml",
            cookies=cookies,
            headers=headers,
            proxies=proxies,
        )
        expires = response.json()["js"]["phone"]
        if expires:
            return expires
    except:
        pass


def getAllChannels(url, mac, token, proxy=None, t_zone=None):
    proxies = {"http": proxy, "https": proxy}
    cookies = {"mac": mac, "stb_lang": "en", "timezone": t_zone}
    headers = {
        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
        "Authorization": "Bearer " + token,
    }
    try:
        response = s.get(
            url
            + "?type=itv&action=get_all_channels&force_ch_link_check=&JsHttpRequest=1-xml",
            cookies=cookies,
            headers=headers,
            proxies=proxies,
        )
        channels = response.json()["js"]["data"]
        if channels:
            return channels
    except:
        pass


def getGenres(url, mac, token, proxy=None, t_zone=None):
    proxies = {"http": proxy, "https": proxy}
    cookies = {"mac": mac, "stb_lang": "en", "timezone": t_zone}
    headers = {
        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
        "Authorization": "Bearer " + token,
    }
    try:
        response = s.get(
            url + "?action=get_genres&type=itv&JsHttpRequest=1-xml",
            cookies=cookies,
            headers=headers,
            proxies=proxies,
        )
        genreData = response.json()["js"]
        if genreData:
            return genreData
    except:
        pass


def getGenreNames(url, mac, token, proxy=None, t_zone=None):
    try:
        genreData = getGenres(url, mac, token, proxy, t_zone)
        genres = {}
        for i in genreData:
            gid = i["id"]
            name = i["title"]
            genres[gid] = name
        if genres:
            return genres
    except:
        pass


def getLink(url, mac, token, cmd, proxy=None, t_zone=None):
    proxies = {"http": proxy, "https": proxy}
    cookies = {"mac": mac, "stb_lang": "en", "timezone": t_zone}
    headers = {
        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
        "Authorization": "Bearer " + token,
    }
    try:
        response = s.get(
            url
            + "?type=itv&action=create_link&cmd="
            + cmd
            + "&series=0&forced_storage=false&disable_ad=false&download=false&force_ch_link_check=false&JsHttpRequest=1-xml",
            cookies=cookies,
            headers=headers,
            proxies=proxies,
        )
        data = response.json()
        link = data["js"]["cmd"].split()[-1]
        if link:
            return link
    except:
        pass


def getEpg(url, mac, token, period, proxy=None, t_zone=None):
    proxies = {"http": proxy, "https": proxy}
    cookies = {"mac": mac, "stb_lang": "en", "timezone": t_zone}
    headers = {
        "User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)",
        "Authorization": "Bearer " + token,
    }
    try:
        response = s.get(
            url
            + "?type=itv&action=get_epg_info&period="
            + str(period)
            + "&JsHttpRequest=1-xml",
            cookies=cookies,
            headers=headers,
            proxies=proxies,
        )
        data = response.json()["js"]["data"]
        if data:
            return data
    except:
        pass


def getToken_fb(url, mac, proxy=None, t_zone=None):
    proxies = {"http": proxy, "https": proxy} if proxy else None
    base = {"mac": mac, "stb_lang": "en", "timezone": t_zone}
    def _extract(resp):
        try:
            j = resp.json()
            if isinstance(j, dict):
                j = j.get("js", j)
                return j.get("token") if isinstance(j, dict) else None
        except Exception:
            return None
        return None
    u = url + "?type=stb&action=handshake&JsHttpRequest=1-xml"
    variants = [
        ({"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)"}, {}),
        ({"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; MAG250; WebKit)"}, {}),
        ({"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "X-User-Agent": "Model: MAG254; Link: WiFi"}, {}),
        ({"User-Agent": "Mozilla/5.0 (QtEmbedded; U; Linux; C)", "Referer": url}, {}),
    ]
    for headers, extra in variants:
        try:
            r = s.get(u, cookies=base, headers=headers, proxies=proxies, timeout=10)
            tok = _extract(r)
            if tok:
                return tok
        except Exception:
            pass
    try:
        r = s.post(base_url, params={"type":"stb","action":"handshake","JsHttpRequest":"1-xml"},
                   cookies=base, headers=variants[0][0], proxies=proxies, timeout=10)
        tok = _extract(r)
        if tok:
            return tok
    except Exception:
        pass
    try:
        alt = {"mac": mac.lower(), "stb_lang":"en", "timezone": (str(t_zone) if t_zone is not None else None)}
        r = s.get(u, cookies=alt, headers=variants[0][0], proxies=proxies, timeout=10)
        tok = _extract(r); 
        if tok:
            return tok
    except Exception:
        pass
    try:
        r = s.get(u + f"&mac={mac}", cookies=base, headers=variants[0][0], proxies=proxies, timeout=10)
        tok = _extract(r); 
        if tok:
            return tok
    except Exception:
        pass
    return None


def getToken_fb_multi(url, mac, proxies_str=None, t_zone=None):
    """Try fb1..fb7 on all proxies (comma/space separated). Returns (token, used_proxy) or (None, None)."""
    import re as _re
    proxies = [None]
    if proxies_str:
        parts = [p.strip() for p in _re.split(r'[\s,]+', str(proxies_str)) if p.strip()]
        if parts: proxies = parts
    for p in proxies:
        tok = getToken_fb(url, mac, proxy=p, t_zone=t_zone)
        if tok:
            return tok, p
    return None, None
