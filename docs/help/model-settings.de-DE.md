# Teamweite Modelleinstellungen verwalten

Unter **Modellzuweisungen → Wissensbasis → Anzahl der Suchergebnisse** sind **15 Ergebnisse** voreingestellt. Wählen Sie 5, 10, 15, 20, 30 oder 50. Die Änderung baut den Index nicht neu auf. Mehr Ergebnisse können die Abdeckung verbessern, benötigen jedoch mehr Modellkontext. Berechtigungs- und Relevanzprüfungen können weniger Treffer liefern. Vorhandene eigene Werte von 1 bis 50 bleiben auswählbar.

Teamverantwortliche und Teamadministratoren verwalten Modelldienste unter **Team-Einstellungen → Modelle**. Die Seite trennt Verbindungsverwaltung, produktive Zuweisungen samt Prüfnachweisen sowie externe Capabilities und den Indexstatus.

## Verbindungen und Modellfunktionen verwalten

Fügen Sie eine Verbindung hinzu oder bearbeiten Sie eine vorhandene. Geben Sie Anbieteradresse, Authentifizierung, erforderliche Parameter, Modell-IDs und unterstützte Funktionen ein. Speichern Sie zuerst die Verbindung und wählen Sie dann **Prüfplan erstellen**, um Funktionen, Anzahl der Anfragen und mögliche Kosten zu prüfen, bevor Sie die Validierung ausführen. Die Validierung kontaktiert den Anbieter und kann Kontingent verbrauchen, speichert jedoch Konfigurationsänderungen nicht automatisch.

Legen Sie im Modellkatalog für jedes Modell die Kontingentfaktoren **Eingabe**, **Zwischengespeicherte Eingabe** und **Ausgabe** fest. Alle drei haben standardmäßig den Wert 1, auch bei bestehenden Modellen. Der Cache-Faktor gilt nur, wenn der Anbieter Cache-Treffer meldet. Erlaubt sind Werte von 0 bis 1.000 mit höchstens sechs Nachkommastellen. Jeder neue Modellaufruf erfasst die zu diesem Zeitpunkt gültigen Faktoren; frühere Aufrufe behalten ihre Werte, auch wenn die Aufgabe weiterläuft. Die Berechnung und die Verbrauchsdetails finden Sie unter [Kontingent prüfen und zuweisen](credits.de-DE.md).

Wenn Sie Adresse, Zugangsdaten, Modell-ID oder Funktionsparameter ändern, können frühere Prüfergebnisse ungültig werden. Ersetzen Sie ein Modell oder eine Verbindung in den produktiven Zuweisungen oder externen Capabilities, bevor Sie es entfernen.

## Ausgabekapazität und Anfragegrenzen

**Modell-Ausgabekapazität (Tokens)** zeigt die dokumentierte Kapazität und ihre Herkunft. Automatische Anfragen erlauben bis zu 256.000 Tokens, bei unbekannter Kapazität 32.000; eine kleinere Modellkapazität und der Platz für die Eingabe begrenzen den tatsächlichen Aufruf. Dies sind Höchstwerte, keine Verbrauchsprognosen. Geben Sie für eigene Deployment-IDs den dokumentierten Wert ein. **Geerbte Kapazität verwenden** entfernt die Modellüberschreibung. Andere Änderungen speichern keinen abgeleiteten Standard.

Das sichtbare Ausgabelimit zeigt automatische, feste, begrenzte oder nicht gesendete Werte je Capability. Anfrageparameter steuern einzelne Aufrufe; eine Kapazitätsangabe setzt kein festes Anfragelimit. Bei ausgelassenem Feld ist das Serverlimit unbekannt. Feste Limits bleiben erhalten und müssen zum Modell und Kontext passen.

Bei abgeschnittener Ausgabe fordert Bowra bis zu zweimal eine kleinere vollständige Aktion an. Der unvollständige Tool-Batch wird nicht ausgeführt; frühere erfolgreiche Schreibvorgänge werden nicht wiederholt. Der laufende Status zeigt die Korrektur. Bleibt die Ausgabe zu lang, bleiben frühere Ergebnisse erhalten. Fordern Sie dann einen kürzeren Bericht an oder lassen Sie die Ausgabelimits prüfen. Zeit- und Schrittgrenzen gelten weiterhin.

## Produktive Modelle zuweisen

Wählen Sie **Zuweisungen bearbeiten**, um primäre, Ersatz-, leichte und Evaluierungsmodelle sowie Modelle für Wissensvektoren, Reranking, Dokumenterkennung und Zeilenextraktion festzulegen. Es stehen nur Modelle aus gespeicherten Verbindungen zur Auswahl. Validieren Sie die für jede ausgewählte Zuweisung benötigten Funktionen und bestätigen Sie anschließend die auf der Seite angezeigten Nachweise.

## Automatische Wiederholungen bei Fehlern

Unter **Modellzuweisungen** enthält **Modellverhalten** eine Einstellung: **Automatische Wiederholungen bei Fehlern**. Schlägt ein Modellaufruf fehl, bevor das Modell überhaupt antwortet (etwa durch Zeitüberschreitung, Verbindungsfehler oder einen vorübergehenden Dienstfehler), wiederholt Bowra den Aufruf bis zu dieser Anzahl. Standard ist 3, zulässig sind 0 bis 5; mit 0 sind Wiederholungen aus. Wählen Sie **Speichern** neben dem Feld; die Einstellung wird separat gespeichert und ändert keine Zuweisungen.

Wiederholt wird nur, solange das Modell noch nicht antwortet, sodass keine Antwort doppelt erscheint. Jede Wiederholung zählt zum Zeitlimit der Runde; reicht die Zeit nicht mehr, endet der Versuch mit einer Fehlermeldung. Während einer Wiederholung zeigt der Laufstatus der Unterhaltung z. B. „Zeitüberschreitung bei der Modellantwort, neuer Versuch (1/3)“. Meldet die Seite, dass sich die Konfiguration geändert hat, laden Sie die Seite neu und legen Sie den Wert erneut fest.

## Externe Capabilities und Indizes

Konfigurieren Sie Anbietermodelle für Suche, Bilderzeugung und andere unterstützte externe Funktionen im jeweils vorgesehenen Bereich. Prüfen Sie den Indexstatus, bevor Sie sich auf einen Wissensdienst verlassen. Eine gespeicherte Verbindung ist dadurch weder automatisch einer produktiven Aufgabe zugewiesen noch einsatzbereit.

Speichern Sie Ihre Änderungen, bevor Sie die Seite verlassen. Öffnen Sie den betreffenden Bereich erneut und prüfen Sie die angezeigten Werte. In einer Unterhaltung stehen nur die Modelle zur Auswahl, die das Team freigegeben hat.
