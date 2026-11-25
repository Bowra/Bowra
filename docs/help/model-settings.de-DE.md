# Teamweite Modelleinstellungen verwalten

Unter **Modellzuweisungen → Wissensbasis → Anzahl der Suchergebnisse** sind **15 Ergebnisse** voreingestellt. Wählen Sie 5, 10, 15, 20, 30 oder 50. Die Änderung baut den Index nicht neu auf. Mehr Ergebnisse können die Abdeckung verbessern, benötigen jedoch mehr Modellkontext. Berechtigungs- und Relevanzprüfungen können weniger Treffer liefern. Vorhandene eigene Werte von 1 bis 50 bleiben auswählbar.

Teamverantwortliche und Teamadministratoren verwalten Modelldienste unter **Team-Einstellungen → Modelle**. Die Seite hat eigene Registerkarten für Modellverbindungen und Modellzuweisungen. **Externe Fähigkeiten** öffnet eine separate Seite für Bilderzeugung und Websuche.

**Quellenbeschreibungen** ist eine optionale, standardmäßig deaktivierte Modellzuweisung. Ein deklariertes Textmodell verarbeitet neue durchsuchbare Veröffentlichungen und ausdrückliche Generierungsaufträge. Speichern verarbeitet keine vorhandenen Dateien; Quellenlesen ruft kein Modell auf. Entfernen stoppt neue Aufgaben; weiterhin gültige Beschreibungen bleiben nutzbar.

## Verbindungen getrennt prüfen und speichern

Nach der Bowra-Autorisierung öffnet sich der Verbindungseditor; ein vorbereiteter Chat-/Tool-Prüfplan wartet unten auf der Seite. Wählen Sie **Ansehen und starten**, prüfen Sie Anfragezahl und mögliche Kosten und wählen Sie dann **Bestätigen und starten**; Anbieteraufrufe starten nicht automatisch. Speichern ist auch vor der Prüfung möglich. Die feste Aktionsleiste hält Speichern, Abbrechen und Prüfen sichtbar; erweiterte Modellparameter beginnen eingeklappt.

Schlägt das Speichern fehl, steht der Grund neben **Speichern**. Hat jemand anderes mit Administratorrechten die Modelleinstellungen während Ihrer Bearbeitung geändert, wählen Sie **Neueste Einstellungen laden (meine Änderungen behalten)** und speichern Sie dann erneut; Ihre Änderungen bleiben erhalten.

Ist die automatische Zuweisungspflege für Ihre Installation aktiviert, füllen frisch geprüfte geeignete Modelle unkonfigurierte Aufgaben nach Verbindungsänderungen oder abgeschlossener Prüfung. Gültige Auswahl bleibt erhalten. Beim Entfernen werden betroffene Zuweisungen geprüft ersetzt oder geleert. Explizit ausgeschaltete optionale Aufgaben bleiben aus; Quellenbeschreibungen sind standardmäßig aus. Vektormodelle wechseln niemals automatisch und behalten ihre Bestätigung zur Neuindizierung. Ohne Hauptmodell sind Unterhaltungen vorübergehend nicht verfügbar. Verwalten Sie einen verworfenen Bowra-Schlüssel bei Bedarf separat in Bowra API Keys.

## Verbindungen und Modellfunktionen verwalten

Fügen Sie eine Verbindung hinzu oder bearbeiten Sie eine vorhandene. Geben Sie Anbieteradresse, Authentifizierung, erforderliche Parameter, Modell-IDs und unterstützte Funktionen ein. Wählen Sie im Verbindungseditor **Verifizieren**. Ein Dialog zeigt die zu prüfenden Funktionen, die Anzahl der Anfragen und mögliche Kosten; mit **Bestätigen und starten** beginnt die Prüfung. Fortschritt und Anzahl stehen unten auf der Seite, das Ergebnis jeder Funktion auf ihrer Schaltfläche am Modell: **Verifiziert**, **Nicht verifiziert**, **Verifizierung fehlgeschlagen** oder **Erneut verifizieren**. Der Grund eines Fehlers steht unter dem Modell; offene Einträge lassen sich unten erneut prüfen. Die Prüfung kontaktiert den Anbieter und kann Kontingent verbrauchen, speichert aber keine Konfiguration.

Nach dem Speichern wird die Änderung direkt übernommen und bestätigt. Eine Bestätigung vor dem Speichern erscheint nur, wenn diese Änderung Ihre Entscheidung braucht, etwa weil eine genutzte Funktion dieser Verbindung fehlgeschlagen oder nicht verifiziert ist oder der Wissensindex neu aufgebaut werden muss. Probleme anderer Verbindungen erscheinen unter **Modellzuweisungen** und halten das Speichern nicht auf.

Legen Sie im Modellkatalog für jedes Modell die Kontingentfaktoren **Eingabe**, **Zwischengespeicherte Eingabe** und **Ausgabe** fest. Alle drei haben standardmäßig den Wert 1, auch bei bestehenden Modellen. Der Cache-Faktor gilt nur, wenn der Anbieter Cache-Treffer meldet. Erlaubt sind Werte von 0 bis 1.000 mit höchstens sechs Nachkommastellen. Jeder neue Modellaufruf erfasst die zu diesem Zeitpunkt gültigen Faktoren; frühere Aufrufe behalten ihre Werte, auch wenn die Aufgabe weiterläuft. Die Berechnung und die Verbrauchsdetails finden Sie unter [Kontingent prüfen und zuweisen](credits.de-DE.md).

Ein bestandenes Ergebnis bleibt gültig, bis sich die zugehörigen Einstellungen ändern; die Verbindungsliste zeigt, wann es verifiziert wurde. Nach einer Änderung von Adresse oder Schlüssel zeigen alle Funktionen der Verbindung **Erneut verifizieren**; geänderte Anfrageparameter eines Modells betreffen nur dessen Funktion; eine neu angekreuzte Funktion ist **Nicht verifiziert**. Kontingentfaktoren und Kontextfenster ändern keine Ergebnisse. Ohne automatische Zuweisungspflege ersetzen Sie betroffene produktive Zuweisungen vor dem Entfernen; mit aktivierter Pflege prüfen Sie die angezeigten Ersetzungen und Löschungen vor dem Anwenden.

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

Unter **Modellzuweisungen** enthält **Modellverhalten** eine Einstellung: **Automatische Wiederholungen bei Fehlern**. Schlägt ein Modellaufruf fehl (etwa durch Zeitüberschreitung, Verbindungsfehler, eine während der Antwort abgebrochene Verbindung oder einen vorübergehenden Dienstfehler), wiederholt Bowra den Aufruf bis zu dieser Anzahl. Standard ist 3, zulässig sind 0 bis 5; mit 0 sind Wiederholungen aus. Wählen Sie **Speichern** neben dem Feld; die Einstellung wird separat gespeichert und ändert keine Zuweisungen.

Bricht die Verbindung ab, nachdem das Modell bereits zu antworten begonnen hat, verschwindet die unvollständige Antwort und das Modell antwortet von vorn, sodass keine Antwort doppelt erscheint. Der abgebrochene Aufruf wird nicht von Ihren Credits abgezogen. Jede Wiederholung zählt zum Zeitlimit der Runde; reicht die Zeit nicht mehr, endet der Versuch mit einer Fehlermeldung. Erhält ein Analyse-Agent oder ein Planschritt auch nach den Wiederholungen keine Antwort, zeigt der Schritt an, ob beim Warten auf das Modell eine Zeitüberschreitung aufgetreten ist oder der Modelldienst nicht verfügbar war; einen Planschritt können Sie später mit **Plan fortsetzen** erneut ausführen. Während einer Wiederholung zeigt der Laufstatus der Unterhaltung z. B. „Zeitüberschreitung bei der Modellantwort, neuer Versuch (1/3)“. Meldet die Seite, dass sich die Konfiguration geändert hat, laden Sie die Seite neu und legen Sie den Wert erneut fest.

## Externe Fähigkeiten und Indizes

Öffnen Sie **Externe Fähigkeiten**, um Bilderzeugung und Websuche einzurichten. Kehren Sie aus den Einstellungen zur Übersicht der externen Fähigkeiten zurück. Von dort bringt Sie **Zurück zur Übersicht** zu der Modellregisterkarte, von der aus Sie gestartet sind. Den Indexstatus können Sie in der Dateiansicht der Wissensbasis prüfen.

Speichern Sie Ihre Änderungen, bevor Sie die Seite verlassen. Öffnen Sie den betreffenden Bereich erneut und prüfen Sie die angezeigten Werte. In einer Unterhaltung stehen nur die Modelle zur Auswahl, die das Team freigegeben hat.

## Wenn die Prüfung keine Antwort erhält

HTTP 200 bedeutet, dass der Dienst geantwortet hat, aber nicht unbedingt mit einer nutzbaren Modellantwort. Leere Inhalte, Denkinhalte ohne Antwort oder ein unpassendes Antwortformat können dieses Ergebnis auslösen. Es bedeutet nicht zwangsläufig einen Verbindungsfehler.

1. Wählen Sie **Antwortdetails anzeigen**. Teamverantwortliche und Administratoren sehen Modell, Abbruchgrund, erkannte Inhaltstypen, gültige Tokenzahlen, ausgewählte Testparameter und Dienstantwort.
2. Wurde das Antwortlimit erreicht, prüfen Sie Ausgabelimit und Denkeinstellungen und wiederholen Sie die Prüfung. Endet die Antwort normal ohne Inhalt, bitten Sie die Administration des Modelldienstes, Modell und Antwortformat zu prüfen. Fehlende Angaben erscheinen als **Vom Dienst nicht angegeben**; die Seite erfindet keine Ursache.
3. Wählen Sie **Diagnosedetails kopieren**, bevor Sie die Verbindung speichern, das Ergebnis schließen oder die Seite neu laden. Abgeschlossene Ergebnisse öffnen sich nicht automatisch erneut. Ist automatisches Kopieren nicht verfügbar, nutzen Sie den Dialog zum manuellen Kopieren.

Die Testparameter enthalten wesentliche Modell-, Ausgabe- und Denkeinstellungen, soweit vorhanden, aber keine Zugangsdaten oder vollständige Anfrage. Die Verbrauchsübersicht zeigt erkannte numerische Tokenzahlen; weitere Felder bleiben im Antwortausschnitt. JSON wird lesbar formatiert; Leerzeichen und Zeilenumbrüche innerhalb von Zeichenfolgen bleiben erhalten. Bekannte Zugangsdaten werden auch bei Wiederholungen in anderen Feldern oder Stream-Ereignissen ausgeblendet. Prüfen Sie den Inhalt vor der Weitergabe. Nach dem Ausblenden werden höchstens 64 KiB der Antwort angezeigt und kopiert.

Für ältere Versuche oder nicht gespeicherte Details wiederholen Sie die Prüfung und kontrollieren vorher Anfragezahl und mögliche Kosten. Das Anzeigen und Kopieren vorhandener Details sendet keine Modellanfrage und speichert keine Konfigurationsänderung.

## Neuaufbau des Wissensindex prüfen

Wenn der Neuaufbau des Teamindex fehlschlägt, öffnen Team-Eigentümer und Administratoren über die Meldung den Wissensdatenbankindex. Prüfen Sie ausstehendes Embedding-Modell und Status vor dem erneuten Start. Falls das Modell geändert werden muss, öffnen Sie die Modellzuordnung der Wissensdatenbank. Der Verarbeitungsstatus einzelner Dateien steht gesondert auf der Wissensseite.
