#!/usr/bin/env python3
"""Tests sans materiel : un faux ecran (SSD1306/SH1106) et de fausses broches verifient que les bons octets partent.
Lancer :  python3 test_ecran.py"""
import sys, time, types
import ecran

# ---------------------------------------------------------------- 1. faux ecran qui relit ce qu'on lui envoie
class FauxEcran:
    nom, morceau = "faux", 16
    def __init__(self, largeur_ram=132):
        self.ram = [[0] * largeur_ram for _ in range(8)]
        self.page = self.col = 0
        self.attend = 0
        self.cmds = []
    def write(self, addr, data):
        assert addr == 0x3C
        data = list(data)
        if data[0] == 0x00:                       # flux de commandes
            for c in data[1:]:
                self.cmds.append(c)
                if self.attend:
                    self.attend -= 1; continue
                if c in (0xD5, 0xA8, 0xD3, 0x8D, 0x20, 0xDA, 0x81, 0xD9, 0xDB):
                    self.attend = 1
                elif 0xB0 <= c <= 0xB7:
                    self.page = c - 0xB0
                elif c <= 0x0F:
                    self.col = (self.col & 0xF0) | c
                elif 0x10 <= c <= 0x1F:
                    self.col = (self.col & 0x0F) | ((c & 0x0F) << 4)
        else:
            assert data[0] == 0x40
            for b in data[1:]:
                self.ram[self.page][self.col] = b; self.col += 1
    def image(self, decalage):
        from PIL import Image
        img = Image.new("1", (128, 64))
        px = img.load()
        for p in range(8):
            for x in range(128):
                v = self.ram[p][x + decalage]
                for b in range(8):
                    px[x, p * 8 + b] = 255 if (v >> b) & 1 else 0
        return img

def donnees(**kw):
    d = {"noeud": True, "blocs": 20964, "temps_tete": time.time() - 640, "t": time.time(), "connexions": 1, "ibd": False,
         "sync": 1.0, "intervalle": 660.0, "ip": "192.168.10.101", "temp": 52.3, "marche": 3 * 86400, "cpu": 14.0,
         "charge": (0.42, 0.38, 0.30), "freq": 2.4,
         "mem": {"total": 8e9, "utilisee": 3.1e9, "swap_total": 2e9, "swap_utilise": 0},
         "disque": {"total": 2e12, "utilise": 4.12e11, "libre": 1.5e12, "pct": 21.5},
         "ck": {"hashrate1m": "7.12T", "Workers": 1, "accepted": 805000, "rejected": 0}}
    d.update(kw)
    return d

ecran.PAGES = list(ecran.PAGES_DISPO)
ecran.BLOC_CIBLE, ecran.BLOC_CIBLE_TEXTE = 25000, "puis part dev. de 2 %"
TOUT = len(ecran.pages_actives())

for puce, decal in (("ssd1306", 0), ("sh1106", 2)):
    f = FauxEcran()
    o = ecran.Oled(f, 0x3C, puce=puce)
    o.init()
    assert 0xAF in f.cmds and f.cmds[0] == 0xAE, "init : l'ecran doit finir allume"
    img = ecran.rendre(0, donnees())
    o.afficher(img)
    assert f.image(decal).tobytes() == img.tobytes(), "image relue differente (%s)" % puce
print("OK  l'image envoyee est relue a l'identique (SSD1306 et SH1106)")

# l'ecran retourne : 0xA0 et 0xC0 au lieu de 0xA1 et 0xC8
f = FauxEcran(); ecran.Oled(f, 0x3C, retourner=True).init()
assert 0xA0 in f.cmds and 0xC0 in f.cmds and 0xA1 not in f.cmds
print("OK  option RETOURNER")

# ---------------------------------------------------------------- 2. les pages
assert TOUT == 8, TOUT
imgs = [ecran.rendre(i, donnees()) for i in range(TOUT)]
assert len({im.tobytes() for im in imgs}) == TOUT and all(im.size == (128, 64) and im.mode == "1" for im in imgs)
cas_limites = (donnees(noeud=False), donnees(blocs=25000), donnees(blocs=30000), donnees(ck={}), donnees(ibd=True, sync=0.4),
               donnees(temp=None, ip=None, marche=None, cpu=None, charge=None, freq=None, mem=None, disque=None),
               donnees(mem={"total": 8e9, "utilisee": 8e9, "swap_total": 0, "swap_utilise": 0}),
               donnees(disque={"total": 4e12, "utilise": 3.99e12, "libre": 1e10, "pct": 99.7}),
               donnees(temp=100.0, cpu=100.0, ip="255.255.255.255", marche=999 * 86400))
for cas in cas_limites:
    for i in range(TOUT):
        ecran.rendre(i, cas)
print("OK  les %d pages se dessinent dans tous les cas (noeud absent, bloc atteint, mesures impossibles...)" % TOUT)

# choix des pages par la variable PAGES
ecran.PAGES = ["etat", "n_importe_quoi", "compte"]
ecran.BLOC_CIBLE = 0
assert ecran.pages_actives() == ["etat"], ecran.pages_actives()          # 'compte' sans BLOC_CIBLE est ignoree
ecran.PAGES = ["n_importe_quoi"]
assert ecran.pages_actives() == ["etat"]                                  # liste vide ou fausse : page par defaut
ecran.PAGES = ["horloge", "cpu"]
assert ecran.pages_actives() == ["horloge", "cpu"] and ecran.rendre(5, donnees()).size == (128, 64)
print("OK  la variable PAGES choisit et ordonne les pages")

# vraies mesures de la machine (Linux)
c = ecran.cpu_pct(); assert c is None or 0 <= c <= 100
m = ecran.memoire_info(); assert m and 0 < m["utilisee"] <= m["total"]
k = ecran.disque_info("/"); assert k and 0 <= k["pct"] <= 100 and k["total"] > 0
assert ecran.disque_info("/n/existe/pas") is None
print("OK  mesures du systeme (processeur, memoire, disque)")

# ---------------------------------------------------------------- 3. fausses broches : on decode l'I2C 'fait main'
class Electrique:
    """Modele tres simple du bus : le maitre (nos 2 broches) et l'ecran qui repond ACK a l'adresse 0x3C."""
    def __init__(self):
        self.sda_m = self.scl_m = 1          # niveau impose par le maitre (1 = relache)
        self.sda_prec, self.scl_prec = 1, 1
        self.octets, self.courant, self.nclk, self.dans = [], 0, 0, False
        self.transactions, self.addr_ok = [], 0x3C
    def _changement(self):
        sda, scl = self.sda_m, self.scl_m
        if scl and self.scl_prec and sda != self.sda_prec:          # SDA bouge pendant que SCL est haut
            if sda == 0:                                             # START
                self.dans, self.nclk, self.courant, self.octets = True, 0, 0, []
            else:                                                    # STOP
                if self.dans:
                    self.transactions.append(self.octets)
                self.dans = False
        if scl and not self.scl_prec and self.dans:                 # front montant de SCL
            self.nclk += 1
            if self.nclk <= 8:
                self.courant = (self.courant << 1) | sda
                if self.nclk == 8:
                    self.octets.append(self.courant)
        if not scl and self.scl_prec and self.dans and self.nclk == 9:   # fin du 9e coup d'horloge (le ACK)
            self.nclk, self.courant = 0, 0
        self.sda_prec, self.scl_prec = sda, scl
    def sda_lu(self):
        if self.dans and self.nclk == 9 and self.scl_m:             # pendant le ACK, l'ecran tire SDA a 0 s'il est concerne
            if len(self.octets) > 1 or (self.octets[0] >> 1) == self.addr_ok:
                return 0
        return self.sda_m

elec = Electrique()
class Ligne:
    def __init__(self, nom): self.nom = nom
    def request(self, consumer=None, type=None): pass
    def set_direction_output(self, v=0): self._poser(v)
    def set_direction_input(self): self._poser(1)
    def _poser(self, v):
        setattr(elec, "sda_m" if self.nom == "sda" else "scl_m", v); elec._changement()
    def get_value(self): return elec.sda_lu() if self.nom == "sda" else elec.scl_m
    def release(self): pass
class Puce:
    def __init__(self, path): self.path = path
    def label(self): return "pinctrl-rp1" if self.path.endswith("4") else "gpio-brcmstb"
    def num_lines(self): return 54 if self.path.endswith("4") else 32
    def get_line(self, n): return Ligne("sda" if n == 2 else "scl")
    def close(self): pass
faux_gpiod = types.ModuleType("gpiod")
faux_gpiod.Chip, faux_gpiod.LINE_REQ_DIR_IN = Puce, 1
sys.modules["gpiod"] = faux_gpiod
import glob
vrai_glob = glob.glob
glob.glob = lambda p: ["/dev/gpiochip0", "/dev/gpiochip4"] if p == "/dev/gpiochip*" else vrai_glob(p)

assert ecran.trouver_puce_gpio() == "/dev/gpiochip4", "doit choisir la puce RP1"
b = ecran.BusGpio()
assert b.niveaux() == (1, 1)
assert b.probe(0x3C) is True and b.probe(0x3D) is False
elec.transactions.clear()
b.write(0x3C, [0x00, 0xAE, 0x40, 0xFF])
assert elec.transactions == [[0x3C << 1, 0x00, 0xAE, 0x40, 0xFF]], elec.transactions
# et un ecran complet a travers les fausses broches
elec.transactions.clear()
o = ecran.Oled(b, 0x3C); o.init(); o.afficher(imgs[0])
data = [t for t in elec.transactions if t[1] == 0x40]
assert len(data) == 8 * 4 and all(len(t) == 2 + 32 for t in data), "8 pages x 4 morceaux de 32 octets"
print("OK  l'I2C fait main avec les broches envoie les bons octets (adresse, commandes, donnees) et lit bien le ACK")
print("TOUS LES TESTS PASSENT")
