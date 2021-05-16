import json
import os
import platform
import time
from base64 import b64encode
from datetime import datetime
from random import choice
from threading import Thread
from typing import Dict, List

import requests
import traceback
from plyer import notification
from selenium.webdriver import ActionChains
from selenium.webdriver import Chrome
from selenium.webdriver.common.by import By
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.support.ui import WebDriverWait
from webdriver_manager.chrome import ChromeDriverManager

from tools.clog import CLogger
from tools.utils import retry_on_failure, remove_prefix

PATH = os.path.dirname(os.path.realpath(__file__))


class ImpfterminService():
    def __init__(self, code: str, plz: str, kontakt: dict):
        self.code = str(code).upper()
        self.splitted_code = self.code.split("-")

        self.plz = str(plz)
        self.kontakt = kontakt
        self.authorization = b64encode(bytes(f":{code}", encoding='utf-8')).decode("utf-8")

        # Logging einstellen
        self.log = CLogger("impfterminservice")
        self.log.set_prefix(f"*{self.code[-4:]} | {self.plz}")

        # Session erstellen
        self.s = requests.Session()
        self.s.headers.update({
            'Authorization': f'Basic {self.authorization}',
            'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 11_2_3) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/89.0.4389.82 Safari/537.36',
        })

        # Ausgewähltes Impfzentrum prüfen
        self.verfuegbare_impfzentren = {}
        self.impfzentrum = {}
        self.domain = None
        if not self.impfzentren_laden():
            quit()

        # Verfügbare Impfstoffe laden
        self.verfuegbare_qualifikationen: List[Dict] = []
        while not self.impfstoffe_laden():
            self.log.warn("Erneuter Versuch in 60 Sekunden")
            time.sleep(60)

        # OS
        self.operating_system = platform.system().lower()

        # Sonstige
        self.terminpaar = None
        self.qualifikationen = []
        self.app_name = str(self)

    def __str__(self) -> str:
        return "ImpfterminService"

    @retry_on_failure()
    def impfzentren_laden(self):
        """Laden aller Impfzentren zum Abgleich der eingegebenen PLZ.

        :return: bool
        """
        url = "https://www.impfterminservice.de/assets/static/impfzentren.json"

        res = self.s.get(url, timeout=15)
        if res.ok:
            # Antwort-JSON umformatieren für einfachere Handhabung
            formatierte_impfzentren = {}
            for bundesland, impfzentren in res.json().items():
                for impfzentrum in impfzentren:
                    formatierte_impfzentren[impfzentrum["PLZ"]] = impfzentrum

            self.verfuegbare_impfzentren = formatierte_impfzentren
            self.log.info(f"{len(self.verfuegbare_impfzentren)} Impfzentren verfügbar")

            # Prüfen, ob Impfzentrum zur eingetragenen PLZ existiert
            self.impfzentrum = self.verfuegbare_impfzentren.get(self.plz)
            if self.impfzentrum:
                self.domain = self.impfzentrum.get("URL")
                self.log.info("'{}' in {} {} ausgewählt".format(
                    self.impfzentrum.get("Zentrumsname").strip(),
                    self.impfzentrum.get("PLZ"),
                    self.impfzentrum.get("Ort")))
                return True
            else:
                self.log.error(f"Kein Impfzentrum in PLZ {self.plz} verfügbar")
        else:
            self.log.error("Impfzentren können nicht geladen werden")
        return False

    @retry_on_failure(1)
    def impfstoffe_laden(self):
        """Laden der verfügbaren Impstoff-Qualifikationen.
        In der Regel gibt es 3 Qualifikationen, die je nach Altersgruppe verteilt werden.

        """

        path = "assets/static/its/vaccination-list.json"

        res = self.s.get(self.domain + path, timeout=15)
        if res.ok:
            res_json = res.json()

            for qualifikation in res_json:
                qualifikation["impfstoffe"] = qualifikation.get("tssname",
                                                                "N/A").replace(" ", "").split(",")
                self.verfuegbare_qualifikationen.append(qualifikation)

            # Ausgabe der verfügbaren Impfstoffe:
            for qualifikation in self.verfuegbare_qualifikationen:
                q_id = qualifikation["qualification"]
                alter = qualifikation.get("age", "N/A")
                intervall = qualifikation.get("interval", " ?")
                impfstoffe = str(qualifikation["impfstoffe"])
                self.log.info(
                    f"[{q_id}] Altersgruppe: {alter} (Intervall: {intervall} Tage) --> {impfstoffe}")
            print("\n")
            return True

        self.log.error("Keine Impfstoffe im ausgewählten Impfzentrum verfügbar")
        return False

    @retry_on_failure()
    def cookies_erneuern(self):
        self.log.info("Browser-Cookies generieren")

        path = "impftermine/service?plz={}".format(self.plz)

        with Chrome(ChromeDriverManager().install()) as driver:
            driver.get(self.domain + path)

            # Queue Bypass
            queue_cookie = driver.get_cookie("akavpwr_User_allowed")
            if queue_cookie:
                self.log.info("Im Warteraum, Seite neuladen")
                queue_cookie["name"] = "akavpau_User_allowed"
                driver.add_cookie(queue_cookie)

                # Seite neu laden
                driver.get(self.domain + path)
                driver.refresh()

            # Klick auf "Auswahl bestätigen" im Cookies-Banner
            # Warteraum-Support: Timeout auf 1 Stunde
            button_xpath = ".//html/body/app-root/div/div/div/div[2]/div[2]/div/div[1]/a"
            button = WebDriverWait(driver, 60 * 60).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()

            # Klick auf "Vermittlungscode bereits vorhanden"
            button_xpath = "/html/body/app-root/div/app-page-its-login/div/div/div[2]/app-its-login-user/" \
                           "div/div/app-corona-vaccination/div[2]/div/div/label[1]/span"
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()

            # Auswahl des ersten Code-Input-Feldes
            input_xpath = "/html/body/app-root/div/app-page-its-login/div/div/div[2]/app-its-login-user/" \
                          "div/div/app-corona-vaccination/div[3]/div/div/div/div[1]/app-corona-vaccination-yes/" \
                          "form[1]/div[1]/label/app-ets-input-code/div/div[1]/label/input"
            input_field = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, input_xpath)))
            action = ActionChains(driver)
            action.move_to_element(input_field).click().perform()

            # Code eintragen
            input_field.send_keys(self.code)
            time.sleep(.1)

            # Klick auf "Termin suchen"
            button_xpath = "/html/body/app-root/div/app-page-its-login/div/div/div[2]/app-its-login-user/" \
                           "div/div/app-corona-vaccination/div[3]/div/div/div/div[1]/app-corona-vaccination-yes/" \
                           "form[1]/div[2]/button"
            button = WebDriverWait(driver, 1).until(
                EC.element_to_be_clickable((By.XPATH, button_xpath)))
            action = ActionChains(driver)
            action.move_to_element(button).click().perform()

            # Maus-Bewegung hinzufügen (nicht sichtbar)
            action.move_by_offset(10, 20).perform()

            # prüfen, ob Cookies gesetzt wurden und in Session übernehmen
            try:
                cookie = driver.get_cookie("bm_sz")
                if cookie:
                    self.s.cookies.clear()
                    self.s.cookies.update({c['name']: c['value'] for c in driver.get_cookies()})
                    self.log.info("Browser-Cookie generiert: *{}".format(cookie.get("value")[-6:]))
                    return True
                else:
                    self.log.error("Cookies können nicht erstellt werden!")
                    return False
            except:
                return False

    @retry_on_failure()
    def login(self):
        """Einloggen mittels Code, um qualifizierte Impfstoffe zu erhalten.
        Dieser Schritt ist wahrscheinlich nicht zwingend notwendig, aber schadet auch nicht.

        :return: bool
        """
        path = f"rest/login?plz={self.plz}"

        res = self.s.get(self.domain + path, timeout=15)
        if res.ok:
            # Checken, welche Impfstoffe für das Alter zur Verfügung stehen
            self.qualifikationen = res.json().get("qualifikationen")

            if self.qualifikationen:
                zugewiesene_impfstoffe = set()

                for q in self.qualifikationen:
                    for verfuegbare_q in self.verfuegbare_qualifikationen:
                        if verfuegbare_q["qualification"] == q:
                            zugewiesene_impfstoffe.update(verfuegbare_q["impfstoffe"])

                self.log.info("Erfolgreich mit Code eingeloggt")
                self.log.info(f"Mögliche Impfstoffe: {list(zugewiesene_impfstoffe)}")
                print(" ")

                return True
            else:
                self.log.warn("Keine qualifizierten Impfstoffe verfügbar")
        else:
            self.log.warn("Einloggen mit Code nicht möglich")
        print(" ")
        return False

    @retry_on_failure()
    def terminsuche(self):
        """Es wird nach einen verfügbaren Termin in der gewünschten PLZ gesucht.
        Ausgewählt wird der erstbeste Termin (!).
        Zurückgegeben wird das Ergebnis der Abfrage und der Status-Code.
        Bei Status-Code > 400 müssen die Cookies erneuert werden.

        Beispiel für ein Termin-Paar:

        [{
            'slotId': 'slot-56817da7-3f46-4f97-9868-30a6ddabcdef',
            'begin': 1616999901000,
            'bsnr': '005221080'
        }, {
            'slotId': 'slot-d29f5c22-384c-4928-922a-30a6ddabcdef',
            'begin': 1623999901000,
            'bsnr': '005221080'
        }]

        :return: bool, status-code
        """

        path = f"rest/suche/impfterminsuche?plz={self.plz}"

        while True:
            res = self.s.get(self.domain + path, timeout=15)
            if not res.ok or 'Virtueller Warteraum des Impfterminservice' not in res.text:
                break
            self.log.info('Warteraum... zZz...')
            time.sleep(30)

        if res.ok:
            res_json = res.json()
            terminpaare = res_json.get("termine")
            if terminpaare:
                # Auswahl des erstbesten Terminpaares
                self.terminpaar = choice(terminpaare)
                self.log.success("Terminpaar gefunden!")

                for num, termin in enumerate(self.terminpaar, 1):
                    ts = datetime.fromtimestamp(termin["begin"] / 1000).strftime(
                        '%d.%m.%Y um %H:%M Uhr')
                    self.log.success(f"{num}. Termin: {ts}")
                return True, 200
            else:
                self.log.info("Keine Termine verfügbar")
        else:
            self.log.error("Terminpaare können nicht geladen werden")
        return False, res.status_code

    @retry_on_failure()
    def termin_buchen(self):
        """Termin wird gebucht für die Kontaktdaten, die beim Starten des
        Programms eingetragen oder aus der JSON-Datei importiert wurden.

        :return: bool
        """

        path = "rest/buchung"

        # Daten für Impftermin sammeln
        data = {
            "plz": self.plz,
            "slots": [termin.get("slotId") for termin in self.terminpaar],
            "qualifikationen": self.qualifikationen,
            "contact": self.kontakt
        }

        res = self.s.post(self.domain + path, json=data, timeout=15)
        if res.status_code == 201:
            msg = "Termin erfolgreich gebucht!"
            self.log.success(msg)
            self._desktop_notification("Terminbuchung:", msg)
            return True
        else:
            data = res.json()
            try:
                error = data['errors']['status']
            except KeyError:
                error = ''
            if 'nicht mehr verfügbar' in error:
                msg = f"Diesen Termin gibts nicht mehr: {error}"
                self.log.error(msg)
                self._desktop_notification("Terminbuchung:", msg)
            else:
                msg = f"Termin konnte nicht gebucht werden: {data}"
                self.log.error(msg)
                self._desktop_notification("Terminbuchung:", msg)
            return False

    @staticmethod
    def run(code: str, plz: str, kontakt: json, check_delay: int = 60):
        """Workflow für die Terminbuchung.

        :param code: 14-stelliger Impf-Code
        :param plz: PLZ des Impfzentrums
        :param kontakt: Kontaktdaten der zu impfenden Person als JSON
        :param check_delay: Zeit zwischen Iterationen der Terminsuche
        :return:
        """

        its = ImpfterminService(code, plz, kontakt)
        its.cookies_erneuern()

        # login ist nicht zwingend erforderlich
        its.login()

        while True:
            termin_gefunden = False
            while not termin_gefunden:
                termin_gefunden, status_code = its.terminsuche()
                if status_code >= 400:
                    its.cookies_erneuern()
                elif not termin_gefunden:
                    time.sleep(check_delay)

            if its.termin_buchen():
                break
            time.sleep(30)

    def _desktop_notification(self, title: str, message: str):
        """
        Starts a thread and creates a desktop notification using plyer.notification
        """

        if 'windows' not in self.operating_system:
            return

        try:
            Thread(target=notification.notify(
                app_name=self.app_name,
                title=title,
                message=message)
            ).start()
        except Exception as exc:
            self.log.error("Error in _desktop_notification: " + str(exc.__class__.__name__)
                           + traceback.format_exc())


def main():
    print("vaccipy 1.0\n")

    # Check, ob die Datei "kontaktdaten.json" existiert
    kontaktdaten_path = os.path.join(PATH, "kontaktdaten.json")
    kontaktdaten_erstellen = True
    if os.path.isfile(kontaktdaten_path):
        daten_laden = input(
            "Sollen die vorhandene Daten aus 'kontaktdaten.json' geladen werden (y/n)?: ").lower()
        if daten_laden != "n":
            kontaktdaten_erstellen = False

    if kontaktdaten_erstellen:
        print("Bitte trage zunächst deinen Impfcode und deine Kontaktdaten ein.\n"
              "Die Daten werden anschließend lokal in der Datei 'kontaktdaten.json' abgelegt.\n"
              "Du musst sie zukünftig nicht mehr eintragen.\n")
        code = input("Code: ")
        plz = input("PLZ des Impfzentrums: ")

        anrede = input("Anrede (Frau/Herr/...): ")
        vorname = input("Vorname: ")
        nachname = input("Nachname: ")
        strasse = input("Strasse: ")
        hausnummer = input("Hausnummer: ")
        wohnort_plz = input("PLZ des Wohnorts: ")
        wohnort = input("Wohnort: ")
        telefonnummer = input("Telefonnummer: +49")
        mail = input("Mail: ")

        # Anführende Zahlen und Leerzeichen entfernen
        telefonnummer = telefonnummer.strip()
        telefonnummer = remove_prefix(telefonnummer, "+49")
        telefonnummer = remove_prefix(telefonnummer, "0")

        kontakt = {
            "anrede": anrede,
            "vorname": vorname,
            "nachname": nachname,
            "strasse": strasse,
            "hausnummer": hausnummer,
            "plz": wohnort_plz,
            "ort": wohnort,
            "phone": f"+49{telefonnummer}",
            "notificationChannel": "email",
            "notificationReceiver": mail,
        }

        kontaktdaten = {
            "code": code,
            "plz": plz,
            "kontakt": kontakt
        }

        with open(kontaktdaten_path, 'w', encoding='utf-8') as f:
            json.dump(kontaktdaten, f, ensure_ascii=False, indent=4)

    else:
        with open(kontaktdaten_path) as f:
            kontaktdaten = json.load(f)

    try:
        code = kontaktdaten["code"]
        plz = kontaktdaten["plz"]
        kontakt = kontaktdaten["kontakt"]
        print(f"Kontaktdaten wurden geladen für: {kontakt['vorname']} {kontakt['nachname']}\n")
    except KeyError as exc:
        print("Kontaktdaten konnten nicht aus 'kontaktdaten.json' geladen werden.\n"
              "Bitte überprüfe, ob sie im korrekten JSON-Format sind oder gebe "
              "deine Daten beim Programmstart erneut ein.")
        raise exc

    ImpfterminService.run(code=code, plz=plz, kontakt=kontakt, check_delay=30)


if __name__ == "__main__":
    main()
