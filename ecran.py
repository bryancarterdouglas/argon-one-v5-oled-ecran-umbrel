#!/usr/bin/env python3
"""Petit ecran OLED du boitier Argon ONE V5 sous Umbrel : infos du Raspberry Pi (et d'un noeud Bitcoin, en option).

Pourquoi ce programme : le script d'Argon a besoin de modifier le systeme (activer l'I2C dans le fichier de demarrage),
ce qu'Umbrel ne laisse pas faire. Ici on ne touche a rien : le programme parle a l'ecran lui-meme, soit par le bus I2C du
noyau s'il existe (/dev/i2c-1), soit directement avec les deux broches du Pi (GPIO 2 = SDA, GPIO 3 = SCL).

Usage :  ecran.py diag     regarde ce qu'il y a sur le Pi (aucune ecriture sur l'ecran)
         ecran.py test     affiche une mire de test pendant 15 secondes
         ecran.py run      affiche les pages, en boucle (mode normal)
         ecran.py apercu   (sur un ordinateur) cree une image de ce que l'ecran affichera
Pages (variable PAGES, separees par des virgules) :
         etat      temperature, adresse IP, duree de marche (comme l'ecran d'origine)
         cpu       utilisation du processeur, charge, frequence
         memoire   memoire et swap
         disque    place utilisee sur le disque
         horloge   heure et date
         noeud     noeud Bitcoin Core ou derive (via son RPC) : blocs, connexions, age du dernier bloc
         minage    ckpool : hashrate, mineurs, parts rejetees
         compte    compte a rebours jusqu'a un bloc (variable BLOC_CIBLE)
Reglages : voir la section REGLAGES juste en dessous.
"""
import base64, glob, json, os, re, socket, sys, time, urllib.request

# ------------------------------------------------------------------ REGLAGES
PAGES = [p.strip() for p in os.environ.get("PAGES", "etat,cpu,memoire,disque,horloge").split(",") if p.strip()]
NOM_NOEUD = os.environ.get("NOM_NOEUD", "NŒUD")                  # titre de la page noeud (ex. RADTHAL)
DISQUE = os.environ.get("DISQUE", "/")                           # dossier dont on mesure la place (ex. /hote)
BLOC_CIBLE = int(os.environ.get("BLOC_CIBLE", "0"))              # page 'compte' : bloc a atteindre (0 = page desactivee)
BLOC_CIBLE_TEXTE = os.environ.get("BLOC_CIBLE_TEXTE", "")        # ex. "puis part dev. de 2 %"
RPC = os.environ.get("RPC", "http://127.0.0.1:8332")             # RPC du noeud (pages noeud et compte)
RPCUSER = os.environ.get("RPCUSER", "bitcoin")
PASSFILE = os.environ.get("PASSFILE", "/run/secrets/rpc")           # fichier contenant le mot de passe RPC
LOGS = os.environ.get("LOGS", "/ckpool/logs")                    # dossier des journaux de ckpool
BUS = os.environ.get("BUS", "auto")                              # auto | noyau | gpio
ADRESSE = int(os.environ.get("ADRESSE", "0x3C"), 0)              # adresse I2C de l'ecran (0x3C ou 0x3D)
PUCE = os.environ.get("PUCE", "ssd1306")                         # ssd1306 | sh1106 (si l'image est decalee de 2 points)
RETOURNER = os.environ.get("RETOURNER", "0") == "1"              # 1 = image a l'envers
CONTRASTE = int(os.environ.get("CONTRASTE", "64"))               # 0 a 255 : plus bas = l'ecran vieillit moins vite
PAGE_SECONDES = float(os.environ.get("PAGE_SECONDES", "6"))
SDA, SCL = int(os.environ.get("SDA", "2")), int(os.environ.get("SCL", "3"))
GPIOCHIP = os.environ.get("GPIOCHIP", "")                        # ex. /dev/gpiochip4 (sinon : trouve tout seul)
DECALAGE = os.environ.get("DECALAGE")                            # colonnes de decalage (2 pour un SH1106)
ADRESSES_ECRAN = (0x3C, 0x3D)                                    # on ne teste JAMAIS 0x1A (ventilateur / alimentation du boitier)

FONTS = ["/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "/usr/share/fonts/TTF"]


# ------------------------------------------------------------------ acces a l'ecran (deux facons)

class BusNoyau:
    """Bus I2C du noyau (/dev/i2c-N), s'il est active sur le Pi."""
    nom, morceau = "noyau", 128

    def __init__(self, chemin):
        import fcntl
        self.fcntl, self.chemin, self.addr = fcntl, chemin, None
        self.fd = os.open(chemin, os.O_RDWR)

    def _choisir(self, addr):
        if self.addr != addr:
            self.fcntl.ioctl(self.fd, 0x0703, addr)           # I2C_SLAVE
            self.addr = addr

    def write(self, addr, data):
        self._choisir(addr)
        os.write(self.fd, bytes(data))

    def probe(self, addr):
        try:
            self._choisir(addr)
            os.write(self.fd, b"\x00")                          # octet de controle seul : sans effet sur l'ecran
            return True
        except OSError:
            return False

    def close(self):
        os.close(self.fd)


def trouver_puce_gpio():
    import gpiod
    if GPIOCHIP:
        return GPIOCHIP
    infos = []
    for p in sorted(glob.glob("/dev/gpiochip*")):
        try:
            c = gpiod.Chip(p)
            infos.append((p, c.label(), c.num_lines()))
            try:
                c.close()
            except Exception:
                pass
        except Exception:
            pass
    for p, lab, n in infos:
        if "rp1" in lab.lower():
            return p
    for p, lab, n in infos:
        if n == 54:
            return p
    raise RuntimeError("aucune puce GPIO du Pi 5 trouvee (puces vues : %s)" % infos)


class BusGpio:
    """I2C 'fait main' avec deux broches du Pi : aucun reglage du systeme necessaire.
    Une broche est 'basse' quand on la force a 0, 'haute' quand on la relache (les resistances du Pi la remontent)."""
    nom, morceau = "gpio", 32

    def __init__(self, sda=SDA, scl=SCL):
        import gpiod
        self.chemin = trouver_puce_gpio()
        self.chip = gpiod.Chip(self.chemin)
        self.sda, self.scl = self.chip.get_line(sda), self.chip.get_line(scl)
        for ligne in (self.sda, self.scl):
            ligne.request(consumer="radthal-ecran", type=gpiod.LINE_REQ_DIR_IN)

    @staticmethod
    def _bas(ligne):
        ligne.set_direction_output(0)

    @staticmethod
    def _haut(ligne):
        ligne.set_direction_input()

    def niveaux(self):
        return self.sda.get_value(), self.scl.get_value()

    def _start(self):
        self._haut(self.sda); self._haut(self.scl); self._bas(self.sda); self._bas(self.scl)

    def _stop(self):
        self._bas(self.sda); self._haut(self.scl); self._haut(self.sda)

    def _octet(self, b, lire_ack=False):
        for i in range(7, -1, -1):
            (self._haut if (b >> i) & 1 else self._bas)(self.sda)
            self._haut(self.scl); self._bas(self.scl)
        self._haut(self.sda)                                    # on relache SDA : l'ecran repond (ACK) en le tirant a 0
        self._haut(self.scl)
        ack = (self.sda.get_value() == 0) if lire_ack else None
        self._bas(self.scl)
        return ack

    def write(self, addr, data):
        self._start()
        self._octet(addr << 1)
        for b in data:
            self._octet(b)
        self._stop()

    def probe(self, addr):
        self._start()
        ack = self._octet(addr << 1, True)
        self._stop()
        return bool(ack)

    def close(self):
        for ligne in (self.sda, self.scl):
            try:
                ligne.release()
            except Exception:
                pass


def ouvrir_bus(mode=None, verbeux=False):
    """Renvoie (bus, adresse) du premier bus ou l'ecran repond, ou leve une erreur claire."""
    mode, essais = mode or BUS, []
    adresses = (ADRESSE,) + tuple(a for a in ADRESSES_ECRAN if a != ADRESSE)
    if mode in ("auto", "noyau"):
        for chemin in sorted(glob.glob("/dev/i2c-*")):
            try:
                b = BusNoyau(chemin)
            except OSError as e:
                essais.append("%s : %s" % (chemin, e)); continue
            for a in adresses:
                if b.probe(a):
                    return b, a
            b.close(); essais.append("%s : pas d'ecran aux adresses 0x3C/0x3D" % chemin)
    if mode in ("auto", "gpio"):
        try:
            b = BusGpio()
        except Exception as e:
            essais.append("gpio : %s" % e)
        else:
            for a in adresses:
                if b.probe(a):
                    return b, a
            b.close(); essais.append("gpio (%s, broches %d/%d) : pas d'ecran aux adresses 0x3C/0x3D" % (b.chemin, SDA, SCL))
    raise RuntimeError("ecran introuvable : " + " ; ".join(essais or ["aucun bus I2C ni GPIO utilisable"]))


# ------------------------------------------------------------------ le circuit de l'ecran (SSD1306 / SH1106)

class Oled:
    L, H = 128, 64

    def __init__(self, bus, addr, puce=PUCE, retourner=RETOURNER, contraste=CONTRASTE, decalage=DECALAGE):
        self.bus, self.addr, self.puce = bus, addr, puce.lower()
        self.retourner, self.contraste = retourner, max(0, min(255, contraste))
        self.decalage = int(decalage, 0) if decalage not in (None, "") else (2 if self.puce == "sh1106" else 0)

    def _cmd(self, *c):
        self.bus.write(self.addr, [0x00] + list(c))

    def init(self):
        self._cmd(0xAE)                                          # ecran eteint pendant le reglage
        self._cmd(0xD5, 0x80); self._cmd(0xA8, 0x3F); self._cmd(0xD3, 0x00); self._cmd(0x40)
        self._cmd(0x8D, 0x14)                                    # pompe de charge interne
        if self.puce == "ssd1306":
            self._cmd(0x20, 0x02)                                # adressage par pages (commun aux deux circuits)
        self._cmd(0xC0 if self.retourner else 0xC8)
        self._cmd(0xA0 if self.retourner else 0xA1)
        self._cmd(0xDA, 0x12); self._cmd(0x81, self.contraste); self._cmd(0xD9, 0xF1); self._cmd(0xDB, 0x40)
        self._cmd(0xA4); self._cmd(0xA6); self._cmd(0xAF)

    def afficher(self, img):
        """img : image Pillow 128x64 en noir et blanc (mode '1')."""
        px = img.load()
        for page in range(8):
            octets = bytearray(self.L)
            for x in range(self.L):
                v = 0
                for b in range(8):
                    if px[x, page * 8 + b]:
                        v |= 1 << b
                octets[x] = v
            d = self.decalage
            self._cmd(0xB0 + page, d & 0x0F, 0x10 | (d >> 4))
            m = self.bus.morceau
            for i in range(0, self.L, m):
                self.bus.write(self.addr, [0x40] + list(octets[i:i + m]))

    def eteindre(self):
        self._cmd(0xAE)


# ------------------------------------------------------------------ les informations (noeud, ckpool, systeme)

def rpc_lot(appels, timeout=4):
    try:
        pw = open(PASSFILE).read().strip()
        auth = "Basic " + base64.b64encode((RPCUSER + ":" + pw).encode()).decode()
        corps = json.dumps([{"jsonrpc": "1.0", "id": i, "method": m, "params": p} for i, (m, p) in enumerate(appels)]).encode()
        req = urllib.request.Request(RPC, corps, {"Content-Type": "application/json", "Authorization": auth})
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.load(r)
        out = [None] * len(appels)
        for d in data:
            if d.get("error") is None:
                out[d["id"]] = d.get("result")
        return out
    except Exception:
        return None


SUF = {"": 1, "K": 1e3, "M": 1e6, "G": 1e9, "T": 1e12, "P": 1e15, "E": 1e18, "Z": 1e21}


def parse_hs(v):
    """'6.71T' (format de ckpool) -> 6.71e12"""
    if isinstance(v, (int, float)):
        return float(v)
    m = re.match(r"^\s*([0-9]*\.?[0-9]+(?:[eE][+-]?[0-9]+)?)\s*([KMGTPEZ]?)", str(v or ""))
    return float(m.group(1)) * SUF[m.group(2)] if m else 0.0


def statut_ckpool():
    out = {}
    try:
        for ligne in open(LOGS + "/pool/pool.status"):
            ligne = ligne.strip()
            if ligne:
                out.update(json.loads(ligne))
    except Exception:
        pass
    return out


def ip_locale():
    try:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.connect(("10.255.255.255", 1))
        ip = s.getsockname()[0]
        s.close()
        return ip
    except Exception:
        return None


def temperature():
    try:
        return int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000.0
    except Exception:
        return None


def duree_marche():
    try:
        return float(open("/proc/uptime").read().split()[0])
    except Exception:
        return None


_CPU = {"prev": None}


def _lire_stat():
    with open("/proc/stat") as f:
        v = [int(x) for x in f.readline().split()[1:]]
    return sum(v[:8]), v[3] + (v[4] if len(v) > 4 else 0)         # total, inactif


def cpu_pct():
    try:
        total, idle = _lire_stat()
        prev = _CPU["prev"]
        if prev is None:                                          # premier appel : deux mesures a 0,5 s d'ecart
            time.sleep(0.5)
            prev, (total, idle) = (total, idle), _lire_stat()
        _CPU["prev"] = (total, idle)
        dt = total - prev[0]
        return max(0.0, min(100.0, 100.0 * (1 - (idle - prev[1]) / dt))) if dt > 0 else None
    except Exception:
        return None


def frequence_ghz():
    try:
        return int(open("/sys/devices/system/cpu/cpu0/cpufreq/scaling_cur_freq").read()) / 1e6
    except Exception:
        return None


def charge():
    try:
        return os.getloadavg()
    except Exception:
        return None


def memoire_info():
    try:
        m = {}
        for l in open("/proc/meminfo"):
            k, v = l.split(":", 1)
            m[k] = int(v.split()[0]) * 1024
        return {"total": m["MemTotal"], "utilisee": m["MemTotal"] - m["MemAvailable"],
                "swap_total": m.get("SwapTotal", 0), "swap_utilise": m.get("SwapTotal", 0) - m.get("SwapFree", 0)}
    except Exception:
        return None


def disque_info(chemin=None):
    try:
        v = os.statvfs(chemin or DISQUE)
        total = v.f_blocks * v.f_frsize
        utilise = (v.f_blocks - v.f_bfree) * v.f_frsize
        libre = v.f_bavail * v.f_frsize
        return {"total": total, "utilise": utilise, "libre": libre, "pct": 100.0 * utilise / (utilise + libre) if utilise + libre else 0}
    except Exception:
        return None


_INTERVALLE = {"t": 0, "v": 600.0}


def intervalle_blocs(blocs, temps_tete):
    """Secondes moyennes entre deux blocs sur les 100 derniers (recalcule toutes les 2 minutes)."""
    if time.time() - _INTERVALLE["t"] < 120 or blocs < 120:
        return _INTERVALLE["v"]
    r = rpc_lot([("getblockhash", [blocs - 100])])
    if r and r[0]:
        h = rpc_lot([("getblockheader", [r[0]])])
        if h and h[0] and temps_tete and temps_tete > h[0]["time"]:
            _INTERVALLE["v"] = (temps_tete - h[0]["time"]) / 100.0
    _INTERVALLE["t"] = time.time()
    return _INTERVALLE["v"]


def collecter():
    d = {"noeud": False, "ip": ip_locale(), "temp": temperature(), "marche": duree_marche(), "cpu": cpu_pct(),
         "charge": charge(), "freq": frequence_ghz(), "mem": memoire_info(), "disque": disque_info(),
         "ck": statut_ckpool() if "minage" in PAGES else {}, "t": time.time()}
    if any(p in PAGES for p in ("noeud", "compte")):
        r = rpc_lot([("getblockchaininfo", []), ("getnetworkinfo", [])])
        if r and r[0]:
            bc, ni = r[0], r[1] or {}
            d.update(noeud=True, blocs=bc.get("blocks"), temps_tete=bc.get("time"), ibd=bc.get("initialblockdownload"),
                     connexions=ni.get("connections"), sync=bc.get("verificationprogress"))
            d["intervalle"] = intervalle_blocs(d["blocs"], d["temps_tete"])
    return d


# ------------------------------------------------------------------ mise en page (128 x 64 points)

def chemin_police(nom):
    for dossier in FONTS:
        p = os.path.join(dossier, nom)
        if os.path.exists(p):
            return p
    return nom


GRAS, MONO = "DejaVuSans-Bold.ttf", "DejaVuSansMono.ttf"


def police(nom, taille):
    from PIL import ImageFont
    try:
        return ImageFont.truetype(chemin_police(nom), taille)
    except Exception:
        return ImageFont.load_default()


def tient(draw, texte, nom, largeur, taille_max, taille_min=9):
    for t in range(taille_max, taille_min - 1, -1):
        f = police(nom, t)
        if draw.textlength(texte, font=f) <= largeur:
            return f
    return police(nom, taille_min)


def nombre(n):
    return "{:,}".format(int(n)).replace(",", " ")


def virgule(x, dec=1):
    return ("%.*f" % (dec, x)).replace(".", ",")


def duree_courte(s):
    s = int(s)
    if s < 90:
        return "%d s" % s
    if s < 5400:
        return "%d min" % round(s / 60)
    if s < 172800:
        return "%d h %02d" % (s // 3600, (s % 3600) // 60)
    return "%d j %d h" % (s // 86400, (s % 86400) // 3600)


def hashrate_txt(x):
    for u in ("H/s", "kH/s", "MH/s", "GH/s", "TH/s", "PH/s"):
        if x < 1000:
            return virgule(x, 2 if x < 100 else 1) + " " + u
        x /= 1000
    return virgule(x, 1) + " EH/s"


def abrege(x):
    x = float(x or 0)
    for u in ("", " k", " M", " G", " T"):
        if abs(x) < 1000:
            return (virgule(x, 0) if x >= 100 or u == "" else virgule(x, 1)) + u
        x /= 1000
    return virgule(x, 0) + " P"


def page(titre, grand, lignes, num, total):
    """Une page : titre en haut, gros chiffre au milieu, deux lignes en bas."""
    from PIL import Image, ImageDraw
    img = Image.new("L", (128, 64), 0)
    d = ImageDraw.Draw(img)
    d.text((0, 0), titre, font=police(GRAS, 10), fill=255)
    droite = "%d/%d" % (num, total)
    d.text((128 - d.textlength(droite, font=police(MONO, 10)), 0), droite, font=police(MONO, 10), fill=255)
    d.line((0, 12, 127, 12), fill=255)
    f = tient(d, grand, GRAS, 122, 26, 12)
    d.text((64 - d.textlength(grand, font=f) / 2, 14), grand, font=f, fill=255)
    for i, l in enumerate(lignes[:2]):
        fl = tient(d, l, MONO, 128, 10, 9)
        d.text((0, 40 + 11 * i), l, font=fl, fill=255)
    return img.point(lambda p: 255 if p > 110 else 0, "1")


def taille(octets):
    go = octets / 1e9
    if go >= 1000:
        return virgule(go / 1000, 1) + " To"
    return (virgule(go, 1) if go < 100 else "%d" % round(go)) + " Go"


JOURS = ("lundi", "mardi", "mercredi", "jeudi", "vendredi", "samedi", "dimanche")
MOIS = ("janvier", "février", "mars", "avril", "mai", "juin", "juillet", "août", "septembre", "octobre", "novembre", "décembre")


def page_etat(d, num, total):
    t = d.get("temp")
    grand = (virgule(t, 1) + " °C") if t is not None else "—"
    marche = ("En marche " + duree_courte(d["marche"])) if d.get("marche") else "En marche ?"
    return page("ÉTAT DU PI", grand, ["IP " + (d.get("ip") or "?"), marche], num, total)


def page_cpu(d, num, total):
    c, ch, f = d.get("cpu"), d.get("charge"), d.get("freq")
    return page("PROCESSEUR", ("%d %%" % round(c)) if c is not None else "—",
                ["Charge " + (" ".join(virgule(x, 2) for x in ch) if ch else "?"),
                 "Fréquence " + ((virgule(f, 1) + " GHz") if f else "?")], num, total)


def page_memoire(d, num, total):
    m = d.get("mem")
    if not m:
        return page("MÉMOIRE", "—", ["Mesure impossible", ""], num, total)
    sw = ("Swap " + taille(m["swap_utilise"]) + " / " + taille(m["swap_total"])) if m["swap_total"] else "Swap : aucun"
    return page("MÉMOIRE", "%d %%" % round(100.0 * m["utilisee"] / m["total"]),
                [taille(m["utilisee"]) + " / " + taille(m["total"]), sw], num, total)


def page_disque(d, num, total):
    k = d.get("disque")
    if not k:
        return page("DISQUE", "—", ["Mesure impossible", ""], num, total)
    return page("DISQUE", "%d %%" % round(k["pct"]), [taille(k["utilise"]) + " / " + taille(k["total"]), "Libre : " + taille(k["libre"])], num, total)


def page_horloge(d, num, total):
    t = time.localtime(d.get("t") or time.time())
    return page("HEURE", time.strftime("%H:%M", t),
                ["%s %d %s" % (JOURS[t.tm_wday], t.tm_mday, MOIS[t.tm_mon - 1]), "%d  ·  %s" % (t.tm_year, time.tzname[0])], num, total)


def page_noeud(d, num, total):
    if not d.get("noeud"):
        return page(NOM_NOEUD, "—", ["Nœud injoignable", "(il démarre peut-être)"], num, total)
    age = d["t"] - d["temps_tete"] if d.get("temps_tete") else None
    l1 = "Connexions : %s" % (d.get("connexions") if d.get("connexions") is not None else "?")
    if d.get("ibd"):
        l2 = "Synchro %d %%" % round(100 * (d.get("sync") or 0))
    else:
        l2 = "Bloc il y a %s" % (duree_courte(max(0, age)) if age is not None else "?")
    return page(NOM_NOEUD, nombre(d["blocs"]), [l1, l2], num, total)


def page_minage(d, num, total):
    ck = d.get("ck") or {}
    if not ck:
        return page("MINAGE", "—", ["Pas de données ckpool", ""], num, total)
    acc, rej = float(ck.get("accepted") or 0), float(ck.get("rejected") or 0)
    pct = 100.0 * rej / (acc + rej) if acc + rej else 0.0
    return page("MINAGE", hashrate_txt(parse_hs(ck.get("hashrate1m"))),
                ["Mineurs : %d" % int(ck.get("Workers") or 0), "Rejetées : %s %%" % virgule(pct, 2)], num, total)


def page_compte(d, num, total):
    titre = "BLOC %s" % nombre(BLOC_CIBLE)
    if not d.get("noeud"):
        return page(titre, "—", ["Nœud injoignable", ""], num, total)
    reste = BLOC_CIBLE - d["blocs"]
    if reste <= 0:
        return page(titre, "ATTEINT", [BLOC_CIBLE_TEXTE[:21], ""], num, total)
    jours = reste * d.get("intervalle", 600.0) / 86400.0
    approx = ("~%d j" % round(jours)) if jours >= 2 else ("~%d h" % max(1, round(jours * 24)))
    return page(titre, nombre(reste), ["blocs restants, %s" % approx, BLOC_CIBLE_TEXTE[:21]], num, total)


PAGES_DISPO = {"etat": page_etat, "cpu": page_cpu, "memoire": page_memoire, "disque": page_disque, "horloge": page_horloge,
               "noeud": page_noeud, "minage": page_minage, "compte": page_compte}


def pages_actives():
    return [p for p in PAGES if p in PAGES_DISPO and (p != "compte" or BLOC_CIBLE > 0)] or ["etat"]


def rendre(i, d):
    actives = pages_actives()
    n = len(actives)
    return PAGES_DISPO[actives[i % n]](d, i % n + 1, n)


# ------------------------------------------------------------------ les commandes

def cmd_diag():
    print("== Diagnostic de l'ecran ==")
    print("python", sys.version.split()[0])
    try:
        import gpiod
        print("gpiod (libgpiod) :", getattr(gpiod, "version_string", lambda: "?")())
    except Exception as e:
        print("gpiod : INDISPONIBLE (%s)" % e)
    print("peripheriques I2C :", sorted(glob.glob("/dev/i2c-*")) or "aucun (l'I2C du noyau n'est pas active : normal sous Umbrel)")
    puces = sorted(glob.glob("/dev/gpiochip*"))
    print("puces GPIO :", puces or "aucune")
    try:
        import gpiod
        for p in puces:
            c = gpiod.Chip(p)
            print("   %s : %s, %d lignes" % (p, c.label(), c.num_lines()))
    except Exception as e:
        print("   (lecture des puces impossible : %s)" % e)
    print("adresse 0x1A (ventilateur du boitier) : volontairement NON testee")
    noyau_ok = False
    for chemin in sorted(glob.glob("/dev/i2c-*")):
        try:
            bn = BusNoyau(chemin)
        except OSError as e:
            print("   %s : ouverture impossible (%s)" % (chemin, e)); continue
        for a in ADRESSES_ECRAN:
            ok = bn.probe(a)
            noyau_ok = noyau_ok or ok
            print("   %s ecran en 0x%02X : %s" % (chemin, a, "REPOND" if ok else "pas de reponse"))
        bn.close()
    if noyau_ok:
        print("le bus I2C du noyau repond : les broches GPIO ne sont pas touchees (on evite de les detourner du bus)")
    else:
        try:
            b = BusGpio()
            print("broches GPIO %d/%d : niveaux au repos (SDA, SCL) = %s  (attendu : (1, 1))" % (SDA, SCL, b.niveaux()))
            for a in ADRESSES_ECRAN:
                print("   ecran en 0x%02X (par les broches) : %s" % (a, "REPOND" if b.probe(a) else "pas de reponse"))
            b.close()
        except Exception as e:
            print("essai par les broches impossible : %s" % e)
    try:
        bus, addr = ouvrir_bus()
        print("=> ECRAN TROUVE : bus %s, adresse 0x%02X" % (bus.nom, addr))
    except Exception as e:
        print("=> %s" % e)


def preparer(mode=None):
    bus, addr = ouvrir_bus(mode)
    print("ecran trouve : bus %s, adresse 0x%02X, circuit %s" % (bus.nom, addr, PUCE))
    o = Oled(bus, addr)
    o.init()
    return o


def cmd_test():
    from PIL import Image, ImageDraw
    o = preparer()
    img = Image.new("L", (128, 64), 0)
    d = ImageDraw.Draw(img)
    d.rectangle((0, 0, 127, 63), outline=255)
    d.rectangle((3, 3, 124, 60), outline=255)
    d.text((10, 8), "ARGON", font=police(GRAS, 18), fill=255)
    d.text((10, 32), "écran OK", font=police(GRAS, 14), fill=255)
    d.polygon([(118, 8), (118, 20), (106, 20)], fill=255)          # triangle en haut a droite : sert a voir le sens de l'image
    o.afficher(img.point(lambda p: 255 if p > 110 else 0, "1"))
    print("mire affichee pendant 15 secondes")
    time.sleep(15)
    o.eteindre()


def cmd_run():
    o, i, dernier, t_data, d = None, 0, None, 0, {}
    while True:
        try:
            if o is None:
                o = preparer()
                dernier = None
            if time.time() - t_data > 5 or not d:
                d, t_data = collecter(), time.time()
            img = rendre(i, d)
            brut = img.tobytes()
            if brut != dernier:
                o.afficher(img)
                dernier = brut
        except Exception as e:
            print("erreur :", e, flush=True)
            o = None
            time.sleep(10)
            continue
        i += 1
        time.sleep(PAGE_SECONDES)


def cmd_apercu(dossier="."):
    from PIL import Image
    global PAGES, BLOC_CIBLE, BLOC_CIBLE_TEXTE
    PAGES = list(PAGES_DISPO)
    BLOC_CIBLE, BLOC_CIBLE_TEXTE = BLOC_CIBLE or 25000, BLOC_CIBLE_TEXTE or "puis part dév. de 2 %"
    d = {"noeud": True, "blocs": 20964, "temps_tete": time.time() - 640, "t": time.time(), "connexions": 1, "ibd": False,
         "sync": 1.0, "intervalle": 660.0, "ip": "192.168.x.x", "temp": 52.3, "marche": 3 * 86400 + 4 * 3600, "cpu": 14.0,
         "charge": (0.42, 0.38, 0.30), "freq": 2.4, "mem": {"total": 8e9, "utilisee": 3.1e9, "swap_total": 2e9, "swap_utilise": 0},
         "disque": {"total": 2e12, "utilise": 4.12e11, "libre": 1.5e12, "pct": 21.5},
         "ck": {"hashrate1m": "7.12T", "Workers": 1, "accepted": 805000, "rejected": 0}}
    n, scale, gap = len(PAGES), 3, 14
    lignes = (n + 1) // 2
    feuille = Image.new("RGB", (2 * 128 * scale + 3 * gap, lignes * 64 * scale + (lignes + 1) * gap), (40, 44, 52))
    for i in range(n):
        e = rendre(i, d).convert("L").resize((128 * scale, 64 * scale), Image.NEAREST)
        e = Image.merge("RGB", [e.point(lambda p: p * 0.93), e.point(lambda p: p * 0.96), e])
        feuille.paste(e, (gap + (i % 2) * (128 * scale + gap), gap + (i // 2) * (64 * scale + gap)))
    chemin = os.path.join(dossier, "apercu-ecran.png")
    feuille.save(chemin)
    print("apercu :", chemin)


if __name__ == "__main__":
    commande = sys.argv[1] if len(sys.argv) > 1 else "run"
    fonctions = {"diag": cmd_diag, "test": cmd_test, "run": cmd_run,
                 "apercu": lambda: cmd_apercu(sys.argv[2] if len(sys.argv) > 2 else ".")}
    if commande not in fonctions:
        sys.exit(__doc__)
    fonctions[commande]()
