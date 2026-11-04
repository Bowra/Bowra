# Teamweite Modelleinstellungen verwalten

Unter **Modellzuweisungen → Wissensbasis → Anzahl der Suchergebnisse** sind **15 Ergebnisse** voreingestellt. Wählen Sie 5, 10, 15, 20, 30 oder 50. Die Änderung baut den Index nicht neu auf. Mehr Ergebnisse können die Abdeckung verbessern, benötigen jedoch mehr Modellkontext. Berechtigungs- und Relevanzprüfungen können weniger Treffer liefern. Vorhandene eigene Werte von 1 bis 50 bleiben auswählbar.

Teamverantwortliche und Teamadministratoren verwalten Modelldienste unter **Team-Einstellungen → Modelle**. Die Seite trennt Verbindungsverwaltung, produktive Zuweisungen samt Prüfnachweisen sowie externe Capabilities und den Indexstatus.

**Quellenbeschreibungen** ist eine optionale, standardmäßig deaktivierte Modellzuweisung. Ein deklariertes Textmodell verarbeitet neue durchsuchbare Veröffentlichungen und ausdrückliche Generierungsaufträge. Speichern verarbeitet keine vorhandenen Dateien; Quellenlesen ruft kein Modell auf. Entfernen stoppt neue Aufgaben; weiterhin gültige Beschreibungen bleiben nutzbar.

## Verbindungen getrennt prüfen und speichern

Nach der Bowra-Autorisierung öffnet sich der Verbindungseditor mit einem vorbereiteten Chat-/Tool-Prüfplan. Prüfen Sie Anfragezahl und mögliche Kosten und wählen Sie erst dann Start; Anbieteraufrufe starten nicht automatisch. Speichern ist auch vor der Prüfung möglich. Die feste Aktionsleiste hält Speichern, Abbrechen und Prüfen sichtbar; erweiterte Modellparameter beginnen eingeklappt.

Ist die automatische Zuweisungspflege für Ihre Installation aktiviert, füllen frisch geprüfte geeignete Modelle unkonfigurierte Aufgaben nach Verbindungsänderungen oder abgeschlossener Prüfung. Gültige Auswahl bleibt erhalten. Beim Entfernen werden betroffene Zuweisungen geprüft ersetzt oder geleert. Explizit ausgeschaltete optionale Aufgaben bleiben aus; Quellenbeschreibungen sind standardmäßig aus. Vektormodelle wechseln niemals automatisch und behalten ihre Bestätigung zur Neuindizierung. Ohne Hauptmodell sind Unterhaltungen vorübergehend nicht verfügbar. Verwalten Sie einen verworfenen Bowra-Schlüssel bei Bedarf separat in Bowra API Keys.

## Verbindungen und Modellfunktionen verwalten

Fügen Sie eine Verbindung hinzu oder bearbeiten Sie eine vorhandene. Geben Sie Anbieteradresse, Authentifizierung, erforderliche Parameter, Modell-IDs und unterstützte Funktionen ein. Wählen Sie vor oder nach dem Speichern **Prüfplan erstellen**, um Funktionen, Anzahl der Anfragen und mögliche Kosten zu prüfen, bevor Sie die Validierung ausführen. Die Validierung kontaktiert den Anbieter und kann Kontingent verbrauchen, speichert jedoch Konfigurationsänderungen nicht automatisch.

Legen Sie im Modellkatalog für jedes Modell die Kontingentfaktoren **Eingabe**, **Zwischengespeicherte Eingabe** und **Ausgabe** fest. Alle drei haben standardmäßig den Wert 1, auch bei bestehenden Modellen. Der Cache-Faktor gilt nur, wenn der Anbieter Cache-Treffer meldet. Erlaubt sind Werte von 0 bis 1.000 mit höchstens sechs Nachkommastellen. Jeder neue Modellaufruf erfasst die zu diesem Zeitpunkt gültigen Faktoren; frühere Aufrufe behalten ihre Werte, auch wenn die Aufgabe weiterläuft. Die Berechnung und die Verbrauchsdetails finden Sie unter [Kontingent prüfen und zuweisen](credits.de-DE.md).

Wenn Sie Adresse, Zugangsdaten, Modell-ID oder Funktionsparameter ändern, können frühere Prüfergebnisse ungültig werden. Ohne automatische Zuweisungspflege ersetzen Sie betroffene produktive Zuweisungen vor dem Entfernen; mit aktivierter Pflege prüfen Sie die angezeigten Ersetzungen und Löschungen vor dem Anwenden.

## Ausgabekapazität und Anfragegrenzen

**Modell-Ausgabekapazität (Tokens)** zeigt die dokumentierte Kapazität und ihre Herkunft. Automatische Anfragen erlauben bis zu 256.000 Tokens, bei unbekannter Kapazität 32.000; eine kleinere Modellkapazität und der Platz für die Eingabe begrenzen den tatsächlichen Aufruf. Dies sind Höchstwerte, keine Verbrauchsprognosen. Geben Sie für eigene Deployment-IDs den dokumentierten Wert ein. **Geerbte Kapazität verwenden** entfernt die Modellüberschreibung. Andere Änderungen speichern keinen abgeleiteten Standard.

Das sichtbare Ausgabelimit zeigt automatische, feste, begrenzte oder nicht gesendete Werte je Capability. Anfrageparameter steuern einzelne Aufrufe; eine Kapazitätsangabe setzt kein festes Anfragelimit. Bei ausgelassenem Feld ist das Serverlimit unbekannt. Feste Limits bleiben erhalten und müssen zum Modell und Kontext passen.

Bei abgeschnittener Ausgabe fordert Bowra bis zu zweimal eine kleinere vollständige Aktion an. Der unvollständige Tool-Batch wird nicht ausgeführt; frühere erfolgreiche Schreibvorgänge werden nicht wiederholt. Der laufende Status zeigt die Korrektur. Bleibt die Ausgabe zu lang, bleiben frühere Ergebnisse erhalten. Fordern Sie dann einen kürzeren Bericht an oder lassen Sie die Ausgabelimits prüfen. Zeit- und Schrittgrenzen gelten weiterhin.

## Produktive Modelle zuweisen

Der Tab **Modellzuweisungen** zeigt Gesprächs- und Wissensaufgaben mit dem tatsächlichen Modell, der Auswahlregel und vorhandenen Prüfnachweisen. Nicht konfigurierte und ungenutzte Aufgaben bleiben sichtbar. Über **Zuweisungen bearbeiten** ändern Sie zuerst Hauptmodell und Ersatzreihenfolge. Leichte Aufgaben, Bewertung, optionale Wissensaufgaben und die Anzahl der Suchergebnisse lassen sich bei Bedarf aufklappen.

**Prüfdetails anzeigen** öffnet Nachweise, Zeitpunkte, letzte Versuche und einzelne Prüfaktionen, ohne den Dienst aufzurufen. Prüfungen können Kosten verursachen und speichern keine Konfiguration. Die Übersicht gespeicherter Zuweisungen und deren Prüfung bleiben auf der übergeordneten Seite. Vor dem Anwenden zeigt die Prüfung alte und neue Zuweisungen sowie passende Nachweise. Erst die Bestätigung speichert den Entwurf; anschließend erscheint eine Erfolgsmeldung. Wiederholungen werden über **Wiederholungen speichern** separat gespeichert.

Wählen Sie **Zuweisungen bearbeiten**, um primäre, Ersatz-, leichte und Evaluierungsmodelle sowie Modelle für Wissensvektoren, Reranking, Dokumenterkennung und Zeilenextraktion festzulegen. Es stehen nur Modelle aus gespeicherten Verbindungen zur Auswahl. Validieren Sie die für jede ausgewählte Zuweisung benötigten Funktionen und bestätigen Sie anschließend die auf der Seite angezeigten Nachweise.

**Zurück zur Übersicht** öffnet den Tab **Modellzuweisungen** der übergeordneten Seite, auch nach einem direkten Aufruf des Editors. Beim Neuladen bleibt der ausgewählte Tab erhalten.

Während der Bearbeitung prüft **Entwurfszuweisungen prüfen** die im Entwurf ausgewählten Modelle. Bestätigen Sie vor dem Start die Anfragenzahl und mögliche Kosten. Die Prüfung wendet keine Zuweisungen an und verwirft den Entwurf nicht. Gleichnamige Verbindungen zeigen ihre Kennungen, damit alte und neue Auswahl unterscheidbar bleiben.

## Automatische Wiederholungen bei Fehlern

Unter **Modellzuweisungen** enthält **Modellverhalten** eine Einstellung: **Automatische Wiederholungen bei Fehlern**. Schlägt ein Modellaufruf fehl, bevor das Modell überhaupt antwortet (etwa durch Zeitüberschreitung, Verbindungsfehler oder einen vorübergehenden Dienstfehler), wiederholt Bowra den Aufruf bis zu dieser Anzahl. Standard ist 3, zulässig sind 0 bis 5; mit 0 sind Wiederholungen aus. Wählen Sie **Speichern** neben dem Feld; die Einstellung wird separat gespeichert und ändert keine Zuweisungen.

Wiederholt wird nur, solange das Modell noch nicht antwortet, sodass keine Antwort doppelt erscheint. Jede Wiederholung zählt zum Zeitlimit der Runde; reicht die Zeit nicht mehr, endet der Versuch mit einer Fehlermeldung. Während einer Wiederholung zeigt der Laufstatus der Unterhaltung z. B. „Zeitüberschreitung bei der Modellantwort, neuer Versuch (1/3)“. Meldet die Seite, dass sich die Konfiguration geändert hat, laden Sie die Seite neu und legen Sie den Wert erneut fest.

## Externe Capabilities und Indizes

Konfigurieren Sie Anbietermodelle für Suche, Bilderzeugung und andere unterstützte externe Funktionen im jeweils vorgesehenen Bereich. Prüfen Sie den Indexstatus, bevor Sie sich auf einen Wissensdienst verlassen. Eine gespeicherte Verbindung ist dadurch weder automatisch einer produktiven Aufgabe zugewiesen noch einsatzbereit.

Speichern Sie Ihre Änderungen, bevor Sie die Seite verlassen. Öffnen Sie den betreffenden Bereich erneut und prüfen Sie die angezeigten Werte. In einer Unterhaltung stehen nur die Modelle zur Auswahl, die das Team freigegeben hat.

## Wenn die Prüfung keine Antwort erhält

HTTP 200 bedeutet, dass der Dienst geantwortet hat, aber nicht unbedingt mit einer nutzbaren Modellantwort. Leere Inhalte, Denkinhalte ohne Antwort oder ein unpassendes Antwortformat können dieses Ergebnis auslösen. Es bedeutet nicht zwangsläufig einen Verbindungsfehler.

1. Wählen Sie **Antwortdetails anzeigen**. Teamverantwortliche und Administratoren sehen Modell, Abbruchgrund, erkannte Inhaltstypen, gültige Tokenzahlen, ausgewählte Testparameter und Dienstantwort.
2. Wurde das Antwortlimit erreicht, prüfen Sie Ausgabelimit und Denkeinstellungen und wiederholen Sie die Prüfung. Endet die Antwort normal ohne Inhalt, bitten Sie die Administration des Modelldienstes, Modell und Antwortformat zu prüfen. Fehlende Angaben erscheinen als **Vom Dienst nicht angegeben**; die Seite erfindet keine Ursache.
3. Wählen Sie **Diagnosedetails kopieren**, bevor Sie die Verbindung speichern, das Ergebnis schließen oder die Seite neu laden. Abgeschlossene Ergebnisse öffnen sich nicht automatisch erneut. Ist automatisches Kopieren nicht verfügbar, nutzen Sie den Dialog zum manuellen Kopieren.

Die Testparameter enthalten wesentliche Modell-, Ausgabe- und Denkeinstellungen, soweit vorhanden, aber keine Zugangsdaten oder vollständige Anfrage. Die Verbrauchsübersicht zeigt erkannte numerische Tokenzahlen; weitere Felder bleiben im Antwortausschnitt. JSON wird lesbar formatiert; Leerzeichen und Zeilenumbrüche innerhalb von Zeichenfolgen bleiben erhalten. Bekannte Zugangsdaten werden auch bei Wiederholungen in anderen Feldern oder Stream-Ereignissen ausgeblendet. Prüfen Sie den Inhalt vor der Weitergabe. Nach dem Ausblenden werden höchstens 64 KiB der Antwort angezeigt und kopiert.

Für ältere Versuche oder nicht gespeicherte Details wiederholen Sie die Prüfung und kontrollieren vorher Anfragezahl und mögliche Kosten. Das Anzeigen und Kopieren vorhandener Details sendet keine Modellanfrage und speichert keine Konfigurationsänderung.
