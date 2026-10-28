# Dateien, Tastenkürzel und Hinweise auf der Unterhaltungsseite

Dieser Artikel beschreibt Ziehen und Ablegen, Tastenkürzel und Hinweise auf der Unterhaltungsseite. Den Aufbau der Seite selbst erklärt [Aufbau der Unterhaltungsseite](conversation-workbench.de-DE.md).

## Agent, Methode oder Ergebnis auswählen

- Wenn Schnellreferenzen verfügbar sind, geben Sie `@` ein, um einen Agenten für diese Nachricht auszuwählen, oder `/`, um einen Skill, ein Ergebnis oder ein Tool auszuwählen. Wählen Sie einen verfügbaren Eintrag im Menü aus, um dessen Tag hinzuzufügen.
- Ein **Ergebnis (Outcome)** beschreibt das gewünschte Resultat. Ein **Plugin** bündelt Tools und Skills für einen Agenten und wird bei dessen Ressourcen verwaltet. Durch die Auswahl eines Ergebnisses wird kein Plugin hinzugefügt.
- Wenn eine Ressource Parameter unterstützt, können Sie diese über das ausgewählte Tag bearbeiten. Ihre Auswahl gilt für diese Nachricht und ändert nicht die gespeicherte Konfiguration des Agenten.

## Dateien in die Unterhaltung ziehen

- Ziehen Sie Dateien von Ihrem Computer in den Unterhaltungsbereich, also in die Spalte mit Nachrichten und Composer. Ein gestrichelter Rahmen erscheint. Sobald **Zum Hinzufügen zu dieser Nachricht hier ablegen** angezeigt wird, lassen Sie die Maustaste los. Die Dateien werden wie bei der Auswahl von **+** unten links im Composer als Anhänge hinzugefügt und hochgeladen.
- Können die Dateien nicht hinzugefügt werden, wird der Rahmen rot und zeigt den Grund an. Beispielsweise sind alle gezogenen Dateien Videos, Audiodateien oder andere nicht unterstützte Formate, die Nachricht enthält bereits zu viele Anhänge oder Anhänge können derzeit nicht hinzugefügt werden. Beim Loslassen wird dann nichts hinzugefügt.
- Wenn Sie mehrere Dateien gleichzeitig ziehen, können Sie sie ablegen, sofern wenigstens einige hinzugefügt werden können. Diese werden wie gewohnt hochgeladen. Nicht unterstützte Dateien erscheinen mit dem Grund als Anhänge im Composer und können dort entfernt werden.
- Eine Nachricht kann bis zu fünf Anhänge enthalten. Für normale Dateien gilt bei Bereitstellungen mit Objektspeicher eine Grenze von 2 GiB, bei lokalem Speicher von 100 MiB. Word- und PowerPoint-Dateien sind auf 32 MiB, ZIP-Archive auf 500 MiB begrenzt. Sie können Tabellen, PDFs, Dokumente, Bilder, Textdateien und weitere gängige Formate hinzufügen.
- Sie können Dateien auch ziehen, während eine Antwort läuft. Sie werden dann an Ihre nächste Nachricht angehängt.
- Im Ergebnisbereich und in Berichten können keine Dateien abgelegt werden. Wenn Sie eine Datei innerhalb eines Berichts ziehen, wird weiterhin die Reihenfolge der Abschnitte geändert.
- Wenn Sie dieselbe Datei (gleicher Name, gleiche Größe) erneut hinzufügen, entsteht kein zweiter Anhang: Der vorhandene Anhang wird kurz umrandet, und Screenreader melden „Diese Datei ist bereits angehängt“.

## Breite der Unterhaltung und Ergebnisse ändern

- Ziehen Sie am Computer den Trenner zwischen Unterhaltung und Ergebnissen.
- Oder verwenden Sie die Tastatur: Drücken Sie Tab, bis der Trenner fokussiert ist. Mit der linken oder rechten Pfeiltaste ändern Sie die Breite schrittweise. Mit Pos1 wird die Unterhaltung so schmal wie möglich, mit Ende so breit wie möglich. Screenreader sagen an, wie viel Platz die Unterhaltung einnimmt.
- Doppelklicken Sie auf den Trenner, um die Standardbreite wiederherzustellen.
- Die gewählte Breite wird in diesem Browser auf diesem Computer gespeichert.

## Ergebnisse vergrößern

- Drücken Sie auf dem Mac ⌘⇧\ (Befehlstaste + Umschalttaste + umgekehrter Schrägstrich) oder unter Windows Strg + Umschalttaste + \, um die Ergebnisse auf den gesamten Arbeitsbereich zu vergrößern. Drücken Sie dieselbe Kombination erneut, um die vorherige Ansicht wiederherzustellen. Der Tooltip der Schaltfläche oben rechts im Ergebnisbereich zeigt das Tastenkürzel ebenfalls an.
- Im vergrößerten Zustand bleibt links ein schmaler Streifen sichtbar. Wählen Sie ihn aus, um zur Unterhaltung zurückzukehren. Esc stellt die Ansicht nicht wieder her, sondern schließt Menüs und Dialoge.
- ⌘\ oder Strg + \ ohne Umschalttaste klappt weiterhin die Navigationsseitenleiste ein oder aus.
- Mit „\“ ist die Taste gemeint, auf der sich der umgekehrte Schrägstrich einer US-Tastatur befindet. Auf anderen Tastaturlayouts kann diese Taste ein anderes Zeichen zeigen. Kann der Browser das Layout erkennen, zeigt der Tooltip der Vergrößerungsschaltfläche das Zeichen Ihrer Tastatur an. Andernfalls steht dort „die Taste \ auf einer US-Tastatur“.
- Das Tastenkürzel funktioniert nicht, solange ein Menü, eine Liste, ein Dialog oder ein Popover geöffnet ist. Schließen Sie es zuerst.
- Das Tastenkürzel funktioniert auf breiten Computerbildschirmen mit Ergebnissen. Safari verwendet ⌘⇧\ für **Alle Tabs anzeigen**. Öffnet diese Kombination in Safari die Tab-Übersicht, verwenden Sie stattdessen die Vergrößerungsschaltfläche.

## Punkt am Ergebnisbereich

- Während Sie in der Unterhaltung bleiben und im Ergebnisbereich ein neuer Berichtsabschnitt, eine Datentabelle oder eine Datei erscheint, wird ein kleiner Punkt auf **Ergebnisse** oben auf Smartphones angezeigt. Am Computer erscheint er auf dem eingeklappten **Ergebnisse**-Streifen rechts. Screenreader lesen „neuer Inhalt“ vor.
- Der Punkt verschwindet, sobald Sie die Ergebnisse öffnen. Aktualisieren Sie die Seite, bevor Sie sie angesehen haben, bleibt der Punkt sichtbar.

## Eine Runde als Testfall mit der Eigentümerin oder dem Eigentümer eines Agents teilen

Hat ein Team-Agent, der jemand anderem gehört, Ihre Frage beantwortet, bietet das Menü **⋯** neben **Kopieren** die Option **Als Testfall mit der Eigentümerin oder dem Eigentümer teilen**. Dann kann die Eigentümerin oder der Eigentümer dieses Agents genau diese Runde, also Ihre Frage und die erhaltene Antwort, in einen Testfall verwandeln, der künftige Antworten des Agents prüft. Nichts anderes aus der Unterhaltung wird geteilt, und Ihre anderen Unterhaltungen bleiben unsichtbar.

- In der Liste erscheinen Ihr Name und der Anfang Ihrer Frage. Die Antwort wird erst gelesen, wenn daraus ein Testfall entworfen wird.
- Zum Zurücknehmen öffnen Sie dasselbe Menü und wählen **Teilen mit der Eigentümerin oder dem Eigentümer beenden**. Die Runde verschwindet aus der Liste. Ein bereits daraus entworfener Testfall ist eine eigene Kopie und bleibt in der Testreihe; wenden Sie sich direkt an die Eigentümerin oder den Eigentümer, wenn er entfernt werden soll.
- Das Teilen endet auch, wenn Sie die Unterhaltung löschen oder ihren Space verlassen.
- Das Menü erscheint nur bei Team-Agents, die Ihnen nicht gehören. Antworten integrierter Agents oder Ihrer eigenen Agents haben kein Menü; Ihre eigenen Unterhaltungen stehen Ihnen ohnehin zur Verfügung.

## Die Antwort eines Team-Agents als schlecht markieren

Antworten eines Team-Agents haben neben **Kopieren** einen Daumen-nach-unten-Button. Nutzen Sie ihn, wenn eine Antwort falsch oder nicht hilfreich war:

1. Wählen Sie den Daumen nach unten. Ein kleiner Dialog öffnet sich. Alles darin ist optional; auch ohne Angaben wird es beim Senden festgehalten.
2. Kreuzen Sie zutreffende Gründe an: **Fakten oder Zahlen stimmen nicht**, **Hat meine Frage nicht beantwortet**, **Wichtiges fehlt**, **Zu knapp**, **Zu langatmig**, **Schwer lesbar oder schlecht formatiert** oder **Sonstiges**. Schreiben Sie bei Bedarf einen Kommentar mit bis zu 500 Zeichen dazu, was Sie erwartet hätten.
3. Gehört der Agent jemand anderem, zeigt der Dialog außerdem **Der Eigentümerin oder dem Eigentümer des Agents erlauben, diese Runde als Testfall zu verwenden (sie oder er sieht die Frage, die Antwort und Ihr Feedback)** mit dem Namen. Das Kästchen ist nicht angekreuzt, sofern Sie die Runde nicht schon geteilt haben. Kreuzen Sie es nur an, wenn die Eigentümerin oder der Eigentümer diese Runde sehen soll: Es ist dieselbe Zustimmung wie **Als Testfall mit der Eigentümerin oder dem Eigentümer teilen** im Menü **⋯**, und dann sind auch Ihre Gründe und Ihr Kommentar sichtbar. Entfernen Sie das Häkchen bei einer bereits geteilten Runde, endet das Teilen.
4. Wählen Sie **Senden**. Der Button bleibt hervorgehoben, und eine Meldung mit **Rückgängig** bestätigt es. Rückgängig nimmt den Daumen nach unten und ein gerade hinzugefügtes Teilen zurück.

- Um den Daumen nach unten später zurückzunehmen, wählen Sie den hervorgehobenen Button erneut. Damit endet auch das Teilen mit der Eigentümerin oder dem Eigentümer; Sie können die Runde im Menü **⋯** erneut teilen.
- Ohne das Kästchen bleibt Ihr Daumen nach unten privat: Die Eigentümerin oder der Eigentümer des Agents sieht ihn nicht.
- Antworten integrierter Agents haben keinen Daumen nach unten.

## Warum „Senden“ nicht verfügbar ist

- Ist **Senden** noch nicht verfügbar, steht unter dem Composer in kleiner Schrift der Grund, zum Beispiel **Sie können senden, sobald die Anhänge hochgeladen sind**. Sobald der Grund entfällt, können Sie senden.
- Screenreader lesen den Grund auch auf der Schaltfläche **Senden** vor.

## Entwürfe für Berichtsänderungen und erneuter Versuch

- Wenn Sie die KI beim Bearbeiten eines Berichts bitten, einen Abschnitt oder ausgewählten Text umzuschreiben, und das Anweisungsfeld mittendrin mit ×, durch einen Klick an eine andere Stelle oder mit Esc schließen, bleibt Ihre Eingabe erhalten, wenn Sie das Feld für denselben Abschnitt erneut öffnen. Anweisungen werden für jeden Abschnitt jedes Berichts getrennt gespeichert und nach dem Senden gelöscht. Nach dem Aktualisieren der Seite bleiben sie nicht erhalten.
- Schlägt eine Überarbeitung fehl, können Sie über **Erneut versuchen** in der Meldungsleiste denselben Text mit derselben Anweisung erneut überarbeiten lassen. Wurde der Text inzwischen geändert, wird die Leiste einfach geschlossen. Wählen Sie den Text aus, den Sie erneut überarbeiten möchten.

## Tastaturbedienung in Berichten

- Wählen Sie beim Bearbeiten eines Berichts Text aus und drücken Sie ⌥⏎ (Option + Eingabetaste auf dem Mac, Alt + Eingabetaste unter Windows), um den Fokus zur Formatierungsleiste darüber zu verschieben. Mit der linken und rechten Pfeiltaste wechseln Sie zwischen den Schaltflächen, mit Eingabetaste oder Leertaste verwenden Sie eine Schaltfläche. Drücken Sie Esc, um zum Text zurückzukehren; die Textauswahl bleibt bestehen. Das funktioniert nicht, solange ein Eingabemethoden-Editor noch Text zusammensetzt.
- Ist das Abschnittsmenü geöffnet, wählen Sie mit den Pfeiltasten nach oben und unten eine Option, führen Sie sie mit Eingabetaste aus oder schließen Sie das Menü mit Esc. Sie können auch nicht verfügbare Optionen ansteuern; Screenreader lesen den Grund vor.
- Drücken Sie bei einem Zitatmarker die Eingabetaste, um die Quelle zu öffnen. Wenn der Fokus auf der Quellenkarte liegt, schließt Esc die Karte und setzt den Fokus auf den Marker zurück. Auch die Quellenzeile unter einem Diagramm lässt sich mit Eingabetaste oder Leertaste öffnen und mit Esc schließen.
