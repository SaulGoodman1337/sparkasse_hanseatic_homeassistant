# fints_own – Sparkasse (FinTS) und Hanseatic Bank (Online)

Home-Assistant-Custom-Integration für den **automatischen Online-Abruf** von Kontoständen:

- **Sparkasse / andere FinTS-Banken:** Bestehendes FinTS/HBCI-Backend bleibt unverändert.
- **Hanseatic Bank:** Zusätzliches, schreibgeschütztes Backend über die JSON-API von „Meine Hanseatic Bank“ (`https://connecthb.hanseaticbank.de`). Funktioniert prinzipiell für Kreditkarte, Tagesgeld und Darlehenskonto. **Keine** manuellen JSON-/CSV-Dateien notwendig.

> **Wichtig:** Die Hanseatic Bank stellt für ihre Kreditkarten keine reguläre FinTS-Schnittstelle bereit. Ihr Webportal nutzt eine **nicht öffentlich dokumentierte API**. Diese Integration setzt deren derzeit bekannten Login- und Abfrageablauf um. Sie ist nicht von der Bank autorisiert und kann bei API-Änderungen ausfallen. Der Abruf wurde mit simulierten API-Antworten getestet; ein Test mit einem echten Hanseatic-Konto wurde **nicht** durchgeführt. Insbesondere ist ein komplett unbeaufsichtigter Dauerbetrieb nicht zugesichert: Die Bank kann bei **jedem neuen Login** eine App-Freigabe oder SMS-TAN anfordern.

## Installation

1. Das Verzeichnis [`custom_components/fints_own/`](custom_components/fints_own) nach `/config/custom_components/fints_own/` kopieren (bestehende Dateien ersetzen).
2. Bestehende Sparkassenkonfiguration unverändert lassen und einen zweiten Eintrag in `configuration.yaml` ergänzen.
3. Home Assistant neu starten. Die Hanseatic-Anbindung führt bei der ersten Aktualisierung einen Online-Login aus. Falls eine App-Freigabe erforderlich ist, die Hanseatic-Bank-App öffnen und den Login freigeben.
4. Die beiden Sensoren `Hanseatic Visa Saldo` und `Hanseatic Visa Onlinestatus` kontrollieren.

### Fehler: `provider` / `login_id` ungültig, Sparkassen-Zugangsdaten erforderlich

Wenn bei `provider: hanseatic` die Felder `bank_identification_number`, `username`, `pin` und `url` als Pflichtfelder verlangt werden, **läuft noch die alte Sparkassen-Version** der Integration. Die neue Konfiguration ist dann nicht das Problem.

1. Im Home-Assistant-Konfigurationsverzeichnis muss **genau dieser Pfad** vorhanden sein: `/config/custom_components/fints_own/sensor.py`. Beim manuellen Kopieren die **Dateien aus** `custom_components/fints_own/` des Repositories direkt in diesen Zielordner kopieren – **nicht** das gesamte Repository als weiteren Unterordner einfügen.
2. **Alle Dateien** der Integration gemeinsam ersetzen, insbesondere `sensor.py`, `hanseatic.py` und `manifest.json`. Eine alte `sensor.py` darf nicht übrigbleiben.
3. Die installierte `sensor.py` muss `SCHEMA_HANSEATIC_USER` enthalten, `hanseatic.py` muss existieren und die `manifest.json` muss Version `1.2.1` (oder neuer) nennen. In Home Assistant mit dem Terminal-Add-on beispielsweise:

   ```sh
   grep -n 'SCHEMA_HANSEATIC_USER' /config/custom_components/fints_own/sensor.py
   grep -n '"version"' /config/custom_components/fints_own/manifest.json
   ls -l /config/custom_components/fints_own/hanseatic.py
   ```

4. **Home Assistant komplett neu starten** (nicht nur YAML-Konfiguration neu laden), damit Python die neue Integration lädt. Danach erst die Konfiguration prüfen. Bei einer HACS-Installation sicherstellen, dass das Repository tatsächlich in `/config/custom_components/fints_own/` aktualisiert wurde.

**Nicht** die Hanseatic-Zugangsdaten durch Sparkassen-Felder ersetzen oder Zugangsdaten in einem GitHub-Issue veröffentlichen.

## Beispiel: Sparkasse + Hanseatic

```yaml
sensor:
  - platform: fints_own
    users:
      # Dein bestehender Sparkassen-Eintrag: unverändert
      - name: Sparkasse
        bank_identification_number: "DEINE_BLZ"
        username: !secret sparkasse_benutzer
        pin: !secret sparkasse_pin
        url: "DEINE_SPARKASSEN_FINTS_URL"
        transaction_days: 30

      # Neue Online-Anbindung: OHNE FinTS-URL und OHNE JSON-Importdatei
      - name: Visa
        provider: hanseatic
        login_id: !secret hanseatic_login_id
        password: !secret hanseatic_password
        transaction_days: 30  # optional; 0 = nur Saldo, keine Umsätze
        auto_reauthenticate: true
        sca_timeout_seconds: 90
        reauth_retry_hours: 12
```

`secrets.yaml`:

```yaml
hanseatic_login_id: "1234567890"    # Beispiel! Echte 10-stellige Login-ID einsetzen
hanseatic_password: "DEIN_PASSWORT"
```

**Hinweis:** Nicht die Kreditkarten-PIN oder die Sparkassen-Zugangsdaten verwenden, sondern die **10-stellige Hanseatic-Bank-Login-ID und das Onlinebanking-Passwort**. Niemals echte Zugangsdaten in Fehlermeldungen, GitHub-Issues oder Chats veröffentlichen.

Wenn dein Hanseatic-Login mehrere Karten/Konten zurückliefert, wähle eine bestimmte Karte mit:

```yaml
        account_number: "DEINE_KONTONUMMER"
```

Ohne `account_number` wird genau ein Kreditkartenkonto bevorzugt; sofern keine Kreditkarte vorhanden ist, ein einzelnes Tagesgeld- oder Darlehenskonto. Sind mehrere Konten desselben Typs vorhanden, gibt die Integration eine Fehlermeldung aus, statt einen falschen Saldo anzuzeigen.

## Automatische Aktualisierung und Freigabe

- Home Assistant liest den Bankkontostand bei der Einrichtung und anschließend **stündlich** über die Bank-API. Die `transaction_days` Tage an Umsätzen werden optional ebenfalls automatisch abgefragt.
- Solange der gespeicherte `access_token` gültig ist, erfolgt **kein neuer Login** und somit keine erneute App-Anfrage. Der Access-Token und ein von der Bank zurückgegebener Geräteschlüssel werden automatisch lokal in `/config/.fints_own/hanseatic_session_*.json` mit Unix-Dateirechten `0600` gespeichert und nach einem Home-Assistant-Neustart wieder geladen. Zusätzlich werden der letzte erfolgreiche Abruf und die letzte erfolgreiche Authentifizierung persistiert. Das Onlinebanking-Passwort steht nur in deiner Home-Assistant-Konfiguration, nicht in dieser Sitzungsdatei.
- Wenn die Sitzung nicht mehr gültig ist, erfolgt bei `auto_reauthenticate: true` automatisch ein neuer Anmeldeversuch. Die Bank kann dafür eine **App-Freigabe verlangen**. Die Integration wartet dann bis zu `sca_timeout_seconds` Sekunden auf die Bestätigung. Ohne Bestätigung wird kein Saldo als aktuell ausgegeben. Damit dein Smartphone nicht mit Push-Anfragen geflutet wird, folgen nach einem fehlgeschlagenen Versuch für `reauth_retry_hours` Stunden keine weiteren automatischen Loginversuche.
- Für eine sofortige erneute Anmeldung trotz Pause den Home-Assistant-Dienst `fints_own.hanseatic_reauthenticate` aufrufen, mit Daten `name: Visa`.
- Für **SMS-TANs** geht der Sensorstatus auf `sms_otp_required`. Danach den Dienst `fints_own.hanseatic_submit_otp` mit `name: Visa` und `otp: "123456"` (echte SMS-TAN) aufrufen. Die TAN wird nicht in der Sitzung gespeichert. Beachte: Ein neuer Home-Assistant-Start während einer offenen SMS-TAN-Anfrage macht sie ungültig.
- Bei `auto_reauthenticate: false` wird niemals automatisch ein neuer Login angestoßen. Dann muss dieser explizit über den Dienst `hanseatic_reauthenticate` gestartet werden. Bereits vorhandene gültige Sitzungen werden weiterhin stündlich verwendet.

**Eine Freigabe nach jedem abgelaufenen Login lässt sich hier nicht umgehen.** Das ist eine Bankvorgabe und wäre bei einer unbeaufsichtigten stündlichen Aktualisierung ggf. ein praktisches Hindernis. Auch ein gespeicherter Geräteschlüssel garantiert **keine** TAN-freien Logins.

### Gespeicherte Hanseatic-Sitzung prüfen (ab v1.2.1)

In **Entwicklerwerkzeuge → Zustände** den Sensor `sensor.hanseatic_visa_onlinestatus` öffnen. Dessen Attribute zeigen:

| Attribut | Bedeutung |
| --- | --- |
| `session_restored` | Beim Start wurde ein Access-Token oder Geräteschlüssel aus der Datei geladen |
| `access_token_cached` | Ein Bank-Access-Token ist im Speicher vorhanden (kann serverseitig bereits abgelaufen sein) |
| `device_token_cached` | Ein registrierter Geräteschlüssel ist im Speicher vorhanden |
| `session_storage_error` | Das Lesen oder Speichern der lokalen Sitzungsdatei ist fehlgeschlagen |
| `last_authenticated_at` | Letzter erfolgreicher **Bank-Login** (wird gespeichert) |
| `last_successful_sync` | Letzter erfolgreicher **Saldoabruf** (wird gespeichert) |

Mit dem Home-Assistant-Terminal kann die Datei **ohne Anzeigen des geheimen Inhalts** geprüft werden:

```sh
ls -l /config/.fints_own/hanseatic_session_*.json
```

Die Datei muss nach erfolgreichem Login existieren; regulär haben die Dateien Rechte `-rw-------`. **Den Inhalt niemals teilen oder in GitHub hochladen:** Er enthält Bank-Zugriffstokens. `session_restored: true` beweist nur, dass die Datei geladen wurde – **nicht**, dass der Bankserver den Token noch akzeptiert. Eine abgelaufene oder vom Server widerrufene Sitzung kann nicht künstlich verlängert werden.

## Sensoren

Bei `name: Visa`:

| Sensor | Datenquelle |
| --- | --- |
| `Hanseatic Visa Saldo` | Direkt aus `initCustomer → accounts → creditAccounts → saldo` |
| `Hanseatic Visa Verfügbarer Kredit` | Falls ein passender Wert in der API-Antwort vorhanden ist; sonst unbekannt |
| `Hanseatic Visa Kreditlimit` | Falls ein passender Wert in der API-Antwort vorhanden ist; sonst unbekannt |
| `Hanseatic Visa Onlinestatus` | `success`, `approval_required`, `sms_otp_required` oder `error` |

Die Saldo-Vorzeichen werden **unverändert von der Bank-API übernommen**; insbesondere erfolgt keine pauschale Vorzeichenumkehr für Kreditkartenschulden. `retrieved_at` bedeutet Zeitpunkt des erfolgreichen Online-Abrufs. Fehlerhafte oder fehlgeschlagene Abfragen liefern `unknown` für den Saldo, statt einen veralteten Wert als aktuell anzuzeigen.

Optional werden Umsatzsummen, bis zu 15 Einzelumsätze und ggf. `transaction_error` am Saldo-Sensor angezeigt. Für Umsätze, die älter als das von der Bank ohne zusätzliche SCA bereitgestellte Fenster sind, kann eine weitere Freigabe nötig sein; diese Integration fordert derzeit keine separate Historien-SCA an. `transaction_days: 0` deaktiviert die Umsatzabfrage vollständig.

## Hintergrund, Lizenz, Sicherheit

Die Authentifizierung und die API-Endpunkte sind anhand dieser quelloffenen MoneyMoney-Erweiterung nachvollzogen:

- [davyd15/moneymoney-hanseaticbank](https://github.com/davyd15/moneymoney-hanseaticbank) (MIT-Lizenz)
- [Hanseatic Bank: Umsätze und Kontoauszüge](https://www.hanseaticbank.de/hilfe/kreditkarte/kreditkarte-umsaetze-kontoauszug)

Die festen OAuth-Client-Kennungen im Code stammen aus dem öffentlich verfügbaren Webportal. Sie sind **keine individuellen Kunden-Zugangsdaten**. Die Integration sendet Zugangsdaten und Tokens ausschließlich an die in der Implementierung fest vorgegebene HTTPS-Domain der Hanseatic Bank; Weiterleitungen sind blockiert. Sie nutzt nur Endpunkte für Authentifizierung, Kontoinformationen und Umsatzabruf, **keine** Überweisungs- oder Zahlungsfunktionen.

Lokale Token-Dateien sind nicht verschlüsselt; sichere daher deine Home-Assistant-Konfiguration, Dateisystem und Backups. Deaktiviere Debug-Logging mit Netzwerk-Headern, wenn du dir über die Inhalte nicht sicher bist.

## Tests

Lokale Tests ohne Bankkonto, im Repository-Stammverzeichnis:

```bash
python -m unittest discover -s tests -v
```

Sie validieren API-Aufrufreihenfolge, gespeicherte Sitzungen, App- und SMS-Freigaben, Sensorverdrahtung und die unveränderte Sparkassen-Konfiguration. **Diese Tests ersetzen keinen echten Online-Test mit einem Hanseatic-Konto.**

### Quellcode und Beiträge

Die Integration liegt unter [`custom_components/fints_own`](custom_components/fints_own); Tests liegen unter [`tests`](tests). Für die Sparkasse ist die ursprüngliche FinTS-Anbindung weiterhin enthalten.
