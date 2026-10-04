# Schnellstart (Deutsch)

Diese Integration verbindet Home Assistant direkt über Bluetooth mit einer JUNG HOME Installation — Licht,
Steckdosen, Jalousien, Taster, Raumthermostate und Melder — ohne JUNG HOME Gateway und ohne Cloud. Die JUNG HOME App
und das Gateway funktionieren weiter wie bisher.

Die ausführliche Anleitung gibt es auf Englisch: [user guide](../user/README.md). Die Integration selbst spricht
Deutsch, wenn Home Assistant auf Deutsch eingestellt ist: Dialoge, Entitäten, Aktionen und Reparaturhinweise
verwenden die Begriffe der deutschen JUNG HOME App (*Taste*, *Wippe*, *Taster*, *Einsatz*, *Szene*). Nur ein Wort
weicht ab: Was die App *Bereich* nennt, heißt hier *Raum*, denn *Bereich* ist in Home Assistant schon vergeben.

## Was du brauchst

- Eine mit der JUNG HOME App eingerichtete Installation.
- Bluetooth in Reichweite: einen Bluetooth-Adapter am Home-Assistant-Rechner oder einen
  [ESPHome Bluetooth Proxy](https://esphome.io/components/bluetooth_proxy.html) (ESP32) mit
  `bluetooth_proxy: active: true`. Die Bluetooth-Integration von Home Assistant muss eingerichtet sein.
- Adapter oder Proxy in Reichweite **eines netzbetriebenen** JUNG Geräts (Taster mit Einsatz, Steckdose, Aktor). Ein
  Gerät genügt: die netzbetriebenen Geräte reichen die Nachrichten an alle anderen weiter. Batteriegeräte zählen nicht,
  sie schlafen.

## Installation

- **Mit HACS:** *HACS → Integrationen → ⋮ → Benutzerdefinierte Repositories*, die Adresse dieses Repositorys mit der
  Kategorie *Integration* hinzufügen, *JUNG HOME Bluetooth Mesh* installieren, Home Assistant neu starten.
- **Von Hand:** `junghome_ble.zip` aus dem neuesten Release in den Ordner `custom_components/` der Konfiguration
  entpacken (so dass die Dateien in `custom_components/junghome_ble/` liegen), Home Assistant neu starten.

## Einrichten

Unter *Einstellungen → Geräte & Dienste → Entdeckt* erscheint oft von selbst eine Karte:

- *JUNG HOME Gateway …*, wenn ein JUNG HOME Gateway im Netzwerk ist: **Hinzufügen** und bestätigen führt direkt zum
  Gateway-Formular unten, mit der Adresse des Gateways schon eingetragen (noch nicht auf echter Hardware geprüft).
- *Bluetooth Mesh …*, wenn eine JUNG HOME Installation in Reichweite ist. Home Assistant bietet jede an, die es
  sieht, auch die eines Nachbarn: nur hinzufügen, wenn es deine ist.

Sonst *Einstellungen → Geräte & Dienste → Integration hinzufügen → JUNG HOME Bluetooth Mesh*. Home Assistant fragt
dann (außer bei der Gateway-Karte), woher der Netzwerk-Export der App kommt — eine der drei Quellen:

1. **Vom JUNG HOME Gateway abrufen** (Gateway-Firmware 2.1 oder neuer): Adresse des Gateways eingeben
   (ein gefundenes Gateway ist schon eingetragen; sonst `junghome.local` oder die IP-Adresse aus der App unter
   *Einstellungen → Gateway*). Dann entweder das Netzwerk-Key-Passwort aus der App eingeben, oder das Feld leer
   lassen und in der App unter *Einstellungen → Gateway → Zugriffsberechtigungen → Offene Anfragen* die Anfrage
   *Home Assistant (Bluetooth Mesh)* innerhalb von drei Minuten bestätigen.
2. **Exportdatei der App hochladen**: in der App *Projekt → Projektübergabe* öffnen, die Projektdatei `JungHome.json`
   speichern oder an dich selbst schicken und im Dialog hochladen. Das geht auch direkt auf dem Handy mit der Home
   Assistant Companion App: die Datei in den Dateien des Handys speichern und im Dialog auswählen (nicht mit jedem
   Handy ausprobiert).
3. **Eine Datei auf dem Home-Assistant-Host verwenden**: `JungHome.json` zum Beispiel nach `/config/junghome/`
   kopieren und den Pfad eingeben.

Den eingeklappten Abschnitt *Erweitert* (Feld *Unsere Unicast-Adresse*, Vorschlag `0D00`) so lassen. Nur ein
zweites Home Assistant an derselben Installation braucht eine eigene Adresse.

> **Den Export geheim halten.** Er enthält alle Schlüssel der Installation: wer die Datei hat, kann jedes Gerät
> steuern und umkonfigurieren. Nicht weitergeben, nirgends hochladen.

## Erste Schritte

- Jedes JUNG Gerät erscheint als Gerät in Home Assistant, mit dem Namen aus der App. Ein Taster erscheint meist als
  mehrere Geräte: der Taster selbst, das Licht, das er schaltet, und seine Tasten.
- Geräte landen beim ersten Einrichten im Bereich, der wie ihr erster Raum in der App heißt; danach lassen sie sich
  frei verschieben.
- Jede Taste hat eine Ereignis-Entität (*Taste A*, …). Für Automatisierungen: *Einstellungen → Automatisierungen &
  Szenen → Automatisierung erstellen → Auslöser hinzufügen → Gerät*, die Tasten wählen, dann z. B. *Taste A
  geklickt*. Klicks und Doppelklicks melden nur Tasten, die in der App mit dem Gateway verbunden sind; mit einem Licht
  verbundene Tasten melden *Ein / Auf gedrückt* und *Aus / Ab gedrückt*.
- Messende Steckdosen liefern Leistung und Energie; den Sensor *Energie* im Energie-Dashboard als Einzelgerät
  hinzufügen.
- Nach Änderungen in der App (neue Geräte, Räume, Szenen): bei Einrichtung über das Gateway holt Home Assistant den
  Export einige Minuten nach der App-Nutzung und alle sechs Stunden selbst (sofort: Schaltfläche *Export vom Gateway
  abrufen* am Gateway-Gerät; noch nicht auf echter Hardware geprüft), sonst den Export neu teilen und hochladen.

## Hilfe

- Englische Anleitung: [user guide](../user/README.md) — mit [FAQ](../user/faq.md) und
  [Maintenance](../user/maintenance.md) (alle Reparaturhinweise).
- Alle Einzelheiten: [reference](../ha-integration.md).
- Probleme bitte im Issue-Tracker des Repositorys melden, am besten mit dem Diagnose-Download (*⋮ → Diagnosedaten
  herunterladen* auf der Seite der Integration; er enthält keine Schlüssel).
