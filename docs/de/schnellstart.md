# Schnellstart (Deutsch)

Diese Integration verbindet Home Assistant direkt über Bluetooth mit einer JUNG HOME Installation — Licht,
Steckdosen, Jalousien, Taster, Raumthermostate und Melder — ohne JUNG HOME Gateway und ohne Cloud. Die JUNG HOME App
und das Gateway funktionieren weiter wie bisher.

Die ausführliche Anleitung gibt es auf Englisch: [user guide](../user/README.md). Die Dialoge der Integration in Home
Assistant sind derzeit ebenfalls englisch.

> **Hinweis zu den Menünamen.** Die Menüs der JUNG HOME App sind hier sinngemäß aus der englischen App übersetzt, mit
> dem englischen Namen in Klammern. In der deutschen App können sie anders heißen — das ist **nicht überprüft**.

## Was Sie brauchen

- Eine mit der JUNG HOME App eingerichtete Installation.
- Bluetooth in Reichweite: einen Bluetooth-Adapter am Home-Assistant-Rechner oder einen
  [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html) (ESP32) mit
  `bluetooth_proxy: active: true`. Die Bluetooth-Integration von Home Assistant muss eingerichtet sein.
- Adapter oder Proxy in Reichweite **eines netzbetriebenen** JUNG Geräts (Taster mit Einsatz, Steckdose, Aktor). Ein
  Gerät genügt: die netzbetriebenen Geräte reichen die Nachrichten an alle anderen weiter. Batteriegeräte zählen nicht,
  sie schlafen.

## Installation

- **Mit HACS:** *HACS → Integrationen → ⋮ → Benutzerdefinierte Repositories*, die Adresse dieses Repositorys mit der
  Kategorie *Integration* hinzufügen, *JUNG HOME (Bluetooth Mesh)* installieren, Home Assistant neu starten.
- **Von Hand:** `junghome_ble.zip` aus dem neuesten Release in den Ordner `custom_components/` der Konfiguration
  entpacken (so dass die Dateien in `custom_components/junghome_ble/` liegen), Home Assistant neu starten.

## Einrichten

Unter *Einstellungen → Geräte & Dienste* erscheint oft von selbst eine Karte *Bluetooth Mesh network …* unter
*Entdeckt*: **Hinzufügen** wählen. Sonst *Einstellungen → Geräte & Dienste → Integration hinzufügen → JUNG HOME
(Bluetooth Mesh)*. Home Assistant fragt dann, woher der Netzwerk-Export der App kommt — eine der drei Quellen:

1. **Vom JUNG HOME Gateway** (*Fetch it from the JUNG HOME Gateway*; Gateway-Firmware 2.1 oder neuer): Adresse des
   Gateways eingeben (`junghome.local` oder die IP-Adresse aus der App unter *Einstellungen → Gateway* (*Settings →
   Gateway*)). Dann entweder das Netzwerkschlüssel-Passwort aus der App eingeben, oder das Feld leer lassen und in der
   App unter *Einstellungen → Gateway → Zugriffsberechtigungen → Offene Anfragen* (*Settings → Gateway → Access
   permissions → Open requests*) die Anfrage *Home Assistant (Bluetooth Mesh)* innerhalb von drei Minuten
   bestätigen.
2. **Export der App hochladen** (*Upload the app's export file*): in der App *Projekt → Per Datei teilen* (*Project →
   Share via file*) öffnen, die Datei `JungHome.json` speichern oder an sich selbst schicken und im Dialog hochladen.
   Das geht auch direkt auf dem Handy mit der Home Assistant Companion App: die Datei in den Dateien des Handys
   speichern und im Dialog auswählen (nicht mit jedem Handy ausprobiert).
3. **Datei auf dem Home-Assistant-Rechner** (*Use a file on the Home Assistant host*): `JungHome.json` zum Beispiel
   nach `/config/junghome/` kopieren und den Pfad eingeben.

Das Feld *Our unicast address* auf `0D00` lassen. Nur ein zweites Home Assistant an derselben Installation braucht
eine eigene Adresse.

> **Den Export geheim halten.** Er enthält alle Schlüssel der Installation: wer die Datei hat, kann jedes Gerät
> steuern und umkonfigurieren. Nicht weitergeben, nirgends hochladen.

## Erste Schritte

- Jedes JUNG Gerät erscheint als Gerät in Home Assistant, mit dem Namen aus der App. Ein Taster erscheint meist als
  mehrere Geräte: der Taster selbst, das Licht, das er schaltet, und seine Tasten.
- Geräte landen beim ersten Einrichten im Bereich, der wie ihr erster Raum in der App heißt; danach lassen sie sich
  frei verschieben.
- Jede Taste hat eine Ereignis-Entität (*Button A*, …). Für Automationen: *Einstellungen → Automationen & Szenen →
  Automation erstellen → Auslöser hinzufügen → Gerät*, die Tasten wählen, dann z. B. *Button A clicked*. Klicks und
  Doppelklicks melden nur Tasten, die in der App mit dem Gateway verbunden sind; mit einem Licht verbundene Tasten
  melden *pressed on / up* und *pressed off / down*.
- Messende Steckdosen liefern Leistung und Energie; den Sensor *Energy* im Energie-Dashboard als Einzelgerät
  hinzufügen.
- Nach Änderungen in der App (neue Geräte, Räume, Szenen): bei Einrichtung über das Gateway holt Home Assistant den
  Export einige Minuten nach der App-Nutzung und alle sechs Stunden selbst (sofort: Taste *Fetch export from gateway*
  am Gateway-Gerät; noch nicht auf echter Hardware geprüft), sonst den Export neu teilen und hochladen.

## Hilfe

- Englische Anleitung: [user guide](../user/README.md) — mit [FAQ](../user/faq.md) und
  [Maintenance](../user/maintenance.md) (alle Reparaturhinweise).
- Alle Einzelheiten: [reference](../ha-integration.md).
- Probleme bitte im Issue-Tracker des Repositorys melden, am besten mit dem Diagnose-Download (*⋮ → Diagnosedaten
  herunterladen* auf der Seite der Integration; er enthält keine Schlüssel).
