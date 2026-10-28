# Wissensdatenbank verwenden

Nachdem Sie Material zur Wissensdatenbank hinzugefügt haben, sucht Bowra bei Fragen im Chat nach relevanten Inhalten. Dateien, die Sie nur speichern möchten, ohne sie in Chats durchsuchen zu lassen, gehören in **Bibliothek → Dateien**. Sie können sie später zur Wissensdatenbank hinzufügen.

## Seitenaufbau

Die Seite der Wissensdatenbank hat vier Inhaltsbereiche. Wählen Sie einen aus, um zu wechseln. Sie stehen als Tabs über dem Inhalt; in schmalen Fenstern werden sie zu einer seitlich wischbaren Reihe von Schaltflächen.

- **Dateien**: alle Dateien der Wissensdatenbank. Hier können Sie Dateien hochladen, suchen, öffnen und verwalten.
- **Dokumentenmatrix**: mehrere Dateien in einer Tabelle zusammenfassen. Jede Spalte enthält eine Frage und jede Zelle eine Antwort mit Quelle.
- **Extraktionsergebnisse**: Inhalte, die mit den einzelnen Extraktionstabellen aus Ihren Dateien extrahiert wurden.
- **Vorlagen**: Hier werden integrierte, gespeicherte und teamweite Vorlagen sowie Extraktionstabellen erstellt und verwaltet.

Die Schaltfläche **Wissen hinzufügen** in der Kopfzeile funktioniert in jeder Ansicht. Sie wechselt zu „Dateien“ und öffnet das Upload-Fenster.

## Sichtbarkeit einer Datei festlegen

Wählen Sie beim Hochladen den Sichtbarkeitsbereich aus:

- **Persönlich**: Nur Sie können die Datei sehen und durchsuchen.
- **Bereich**: Mitglieder des aktuellen Bereichs können die Datei durchsuchen. Nach einem Bereichswechsel sehen Sie das Material des neuen Bereichs.
- **Team**: Alle Personen im Team können die Datei durchsuchen. Nur Teamadministratoren dürfen Dateien im Team-Bereich hochladen.

## Dateien hochladen

1. Öffnen Sie **Bibliothek → Wissensdatenbank** und wählen Sie oben rechts **Wissen hinzufügen**.
2. Wählen Sie einen Bereich und ziehen Sie jeweils eine Datei hinein oder wählen Sie eine Datei aus. Mehrere Dateien können Sie einzeln hochladen oder in einer ZIP-Datei zusammenfassen. Unterstützt werden PDF, Word, PowerPoint, Excel, CSV, Markdown, TXT, Bilder und ZIP-Dateien mit bis zu 200 MB pro Datei. Nicht unterstützte Dateien im ZIP werden übersprungen; der Grund erscheint im Importergebnis. Ordner werden nicht als Dateien importiert.
3. Eine Meldung bestätigt den Upload. Die Datei erscheint in der Liste und wird verarbeitet. Sobald ihr Status **Verfügbar** lautet, können Chats sie durchsuchen.

Wenn Sie eine leere oder mehr als 200 MB große Datei auswählen, erklärt der Upload-Dialog, wie Sie fortfahren können. Wählen Sie anschließend eine andere Datei aus.

Bei Excel- und CSV-Dateien sind nur die ersten 5.000 Zeilen jeder Datei durchsuchbar; dabei werden alle Tabellenblätter zusammengezählt. Weitere Zeilen werden nicht in die Wissensdatenbank übernommen. Antworten, die eine solche Tabelle zitieren, weisen darauf hin, dass nur die ersten 5.000 Zeilen verwendet wurden. Wenn Sie alle Daten benötigen, teilen Sie die Tabelle vor dem Upload auf mehrere Dateien auf.

## Dateien aus „Dateien“ hinzufügen

In **Bibliothek → Dateien** zeigt die Spalte **Wissensdatenbank**, welchen Status eine Datei dort hat:

- **Hinzugefügt · Status**: Die Datei ist in der Wissensdatenbank. Unter „Dateistatus“ finden Sie die Erklärung. Wählen Sie **In Wissensdatenbank ansehen**, **Fortschritt anzeigen** oder **In Wissensdatenbank erneut versuchen**, um die Datei dort zu öffnen.
- **Für Wissensdatenbank vorgeschlagen** oder **Nicht hinzugefügt**: Wählen Sie **Zur Wissensdatenbank hinzufügen**, legen Sie fest, wer die Datei durchsuchen darf (persönlich, aktueller Bereich oder Team), und bestätigen Sie. Nach dem Hinzufügen führt **Fortschritt anzeigen** in der Meldung direkt zur Datei.

Beim Hinzufügen zur Wissensdatenbank wird die Datei nicht verschoben oder kopiert. Sie bleibt unter „Dateien“.

Dateien, die Sie unter „Dateien“ hochladen und die als langfristiges Referenzmaterial erkannt werden (zum Beispiel Preislisten oder Richtlinien), werden automatisch Ihrer persönlichen Wissensdatenbank hinzugefügt und verarbeitet, damit die KI sie bei passenden Fragen heranziehen kann. Hat Ihr Team die Wissensdatenbank nicht aktiviert oder kein Wissensdatenbank-Modell eingerichtet, bleibt die Datei nur unter „Dateien“. Um eine automatische Aufnahme rückgängig zu machen, wählen Sie **Aus Wissensdatenbank entfernen**. Die Datei selbst bleibt unverändert.

## Dateistatus

| Status           | Bedeutung                                                                                                                                                                                                                                                                                 |
| ---------------- | ----------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------------- |
| Wird hochgeladen | Die Datei wird noch übertragen.                                                                                                                                                                                                                                                           |
| Warteschlange    | Die Datei wartet auf die Verarbeitung. Bei vielen Dateien kann dies dauern.                                                                                                                                                                                                               |
| In Verarbeitung  | Der Inhalt wird gelesen und geordnet. Große oder eingescannte Dateien brauchen länger.                                                                                                                                                                                                    |
| Verfügbar        | Die Verarbeitung ist abgeschlossen; Chats können die Datei durchsuchen.                                                                                                                                                                                                                   |
| Fehlgeschlagen   | Die Verarbeitung wurde nicht abgeschlossen. Halten Sie den Mauszeiger über den Status, um den Grund zu sehen. Wenn Sie die Datei verwalten dürfen, nutzen Sie die Schaltfläche zum erneuten Versuch daneben, oder öffnen Sie die Datei, um den Grund zu lesen und es erneut zu versuchen. |

**Verfügbar** bedeutet, dass die grundlegenden Inhalte durchsucht werden können. Ergebnisse einer strukturierten Tabellenextraktion können später erscheinen.

Wenn eine Datei fehlschlägt, richten Sie sich nach der Meldung:

- **Dieses Dateiformat wird nicht unterstützt**: Konvertieren Sie die Datei in eines der oben genannten Formate und laden Sie sie erneut hoch.
- **Es wurde kein durchsuchbarer Text gefunden**: Prüfen Sie, ob die Datei leer ist oder nur Bilder ohne Text enthält.
- **Verarbeitung unterbrochen** oder **Zeitüberschreitung bei der Verarbeitung**: Wählen Sie **Erneut versuchen**. Teilen Sie sehr große Dateien vor dem Upload auf.
- **Der Verarbeitungsdienst ist vorübergehend nicht verfügbar**: Versuchen Sie es später erneut. Falls das Problem bestehen bleibt, wenden Sie sich an die Teamadministration.
- **Die Originaldatei fehlt**: Diese Datei kann nicht mehr verarbeitet werden. Löschen Sie sie und laden Sie sie erneut hoch.

## Dateien finden

- Wechseln Sie über der Liste zwischen „Persönlich“, „Bereich“ und „Team“. Dazu dient der Filter **Geltungsbereich** in der Werkzeugleiste (auf dem Smartphone unter **Filter**). „Bereich“ zeigt den Namen des aktuellen Bereichs; ein kurzer Hinweis erklärt, wer diesen Bereich durchsuchen darf.
- Bietet der Filter **Geltungsbereich** die Option **Alle**, sehen Sie persönliche Dateien, Dateien des aktuellen Bereichs und Team-Dateien, die Sie durchsuchen können, auf einmal; die Spalte **Geltungsbereich** zeigt, wohin jede Datei gehört. Die Seite merkt sich Ihre letzte Auswahl; beim ersten Öffnen zeigt sie den aktuellen Bereich.
- Suchen Sie im Suchfeld nach Dateinamen.
- Aktivieren Sie **Tiefensuche**, um Dateiinhalte im aktuellen Bereich zu durchsuchen. Neben dem Suchfeld steht, wie viele Dateien und in welchem Bereich gefunden wurden. Jedes Ergebnis zeigt die passende Textstelle und ihre Position (Seite, Abschnitt oder Absatz).
- Wenn die Tiefensuche nichts findet, probieren Sie andere Begriffe, suchen Sie in einem anderen Bereich oder wählen Sie **Nach Dateiname suchen**, um die Tiefensuche zu deaktivieren.
- Filtern Sie nach Status, Dateityp und Upload-Datum. Jede Filterschaltfläche zeigt die gewählten Werte; um eine ganze Gruppe aufzuheben, öffnen Sie ihr Menü und wählen **Status zurücksetzen** oder **Typ zurücksetzen**.
- Sind zwei oder mehr Bedingungen gewählt (Suchbegriff, jeder Status, jeder Typ und das Datum zählen einzeln), erscheinen sie unter der Symbolleiste. Entfernen Sie sie einzeln mit × oder wählen Sie **Alle löschen**. Der Geltungsbereich wird dort nicht aufgeführt.
- Auf dem Smartphone öffnet **Filter** ein Fenster vom unteren Rand mit den Gruppen Geltungsbereich, Status, Typ und Datum. Eine Auswahl gilt sofort; mit **N Dateien anzeigen** kehren Sie zur Liste zurück. Sortierung und **Tiefensuche** bleiben in der Symbolleiste.
- Nach einem erfolgreichen Upload wechselt die Liste in den Geltungsbereich, in den Sie hochgeladen haben (bei **Alle** bleibt sie auf **Alle**), setzt Suche und Filter zurück und zeigt die erste Seite, sortiert nach dem neuesten Upload – Sie müssen die neue Datei nicht suchen. Wenn Dateien aus einer ZIP-Datei noch verarbeitet werden, wählen Sie **Zurück zur Dateiliste**, um ihren Status dort weiterzuverfolgen. Nach Abschluss der Verarbeitung wählen Sie **Abgeschlossen**; die Liste wird auf dieselbe Weise aktualisiert.

## Dateidetails

Wählen Sie eine Datei in der Liste aus, um Status, Sichtbarkeit, hochladende Person, Upload-Zeit und Größe zu sehen.

- Wenn eine Datei verfügbar ist, können Sie **Im Chat verwenden** wählen, um einen neuen Chat mit angehängter Datei zu starten. Diese Option wird für Dateien angeboten, die Sie im aktuellen Bereich hochgeladen haben. Andere Dateien werden in Chats weiterhin automatisch durchsucht.
- Wurde eine Datei in Chats zitiert, erscheint die Anzahl der Zitate unter ihrem Namen.
- Wenn Sie eine Datei aus den Ergebnissen der Tiefensuche öffnen, wird die passende Textstelle zuerst angezeigt und hervorgehoben.
- In einer Dokumentenmatrix öffnet **Quelle ansehen** in einer Zelle die Dateidetails direkt auf der Seite und zeigt zuerst die zitierte Stelle. Schließen Sie die Details, um zur Matrix zurückzukehren.
- Bei einer noch verarbeiteten Datei wird der Fortschritt angezeigt. Sie können die Details schließen; die Verarbeitung wird fortgesetzt. Bei einem Fehler erklären die Details den Grund und bieten **Erneut versuchen** an.
- Wenn Sie die nötige Berechtigung haben, können Sie die Sichtbarkeit ändern, die Datei in eine Tabelle extrahieren oder sie in den Details löschen.

## Dieselben Fragen mit einer Dokumentenmatrix an viele Dateien stellen

1. Wählen Sie **Dokumentenmatrix** und dann **Neue Matrix**. Wählen Sie die Dateien aus. Alternativ können Sie unter „Dateien“ mehrere Dateien auswählen und **Als Matrix öffnen** wählen.
2. Fügen Sie eine Fragespalte hinzu (zum Beispiel „Wie hoch ist der Vertragswert?“), wählen Sie den Antworttyp und starten Sie den Durchlauf. Jede Zeile steht für eine Datei, jede Zelle enthält die Antwort auf die Frage für diese Datei.
3. Wählen Sie eine Zelle aus, um Antwort und Quelle anzusehen. Sie können die Antwort bearbeiten oder als geprüft markieren. **Quelle ansehen** öffnet die Dateidetails auf derselben Seite und zeigt zuerst die betreffende Textstelle.
4. Die Durchläufe verbrauchen abhängig von der Dokumentlänge Kontingent. Vor dem Start sehen Sie eine Schätzung.

Eine häufig verwendete Gruppe von Fragespalten können Sie in den Spalteneinstellungen der Matrix **als Vorlage speichern**. Unter „Vorlagen“ können Sie dann **Als Matrix verwenden** wählen, um alle Spalten auf einmal hinzuzufügen. Eigene Vorlagen lassen sich mit **In Team-Bibliothek veröffentlichen** für das Team freigeben. Mitglieder anderer Bereiche können sie mit **In meinen Bereich kopieren** übernehmen. Veröffentlicht werden nur die Fragespalten; Dateien und Antworten verlassen Ihren Bereich nicht.

## Felder aus Dateien in Tabellen extrahieren

Bei mehreren ähnlichen Dateien, etwa monatlichen Gewinn- und Verlustrechnungen oder Verträgen, können Sie eine **Extraktionstabelle** mit den auszulesenden Feldern erstellen, zum Beispiel Umsatz und Nettogewinn. Jede zur Tabelle hinzugefügte Datei wird mit diesen Feldern als Zeile extrahiert. Sie können die Ergebnisse in der Wissensdatenbank ansehen und direkt im Chat abfragen.

1. Wählen Sie in der Wissensdatenbank **Vorlagen**, dann **Neue Vorlage** und **Extraktionstabelle (bei Upload extrahieren)**. Beschreiben Sie die Felder in einem Satz, prüfen Sie sie und speichern Sie.
2. Wählen Sie in den Dateidetails oder im Menü „Weitere Optionen“ der Liste **In Tabelle extrahieren** und beantworten Sie die Frage „In welche Tabelle soll diese Datei extrahiert werden?“. Sie können auch mehrere Dateien auswählen und gemeinsam extrahieren. Beim Upload einer Excel-Datei können Sie die Tabelle direkt unter **Extraktionstabelle** auswählen.
3. Öffnen Sie nach Abschluss der Extraktion **Extraktionsergebnisse**. Jede Zelle verweist zurück auf den Quelltext.
4. Um eine Datei nicht weiter zu extrahieren, wählen Sie unter **In Tabelle extrahieren** die Option **Nicht extrahieren**. Die Datei bleibt für die Volltextsuche verfügbar.

Die Extraktion wird innerhalb eines Tageskontingents in eine Warteschlange gestellt. Bei vielen Dateien kann die Verarbeitung dauern. „Vorlage“ bezeichnet hier nur eine wiederverwendbare Gruppe von Fragespalten für eine Matrix: Speichern Sie eine Matrix als Vorlage und wenden Sie sie auf eine neue Matrix an.

## Quellen in Antworten ansehen

Wenn eine Chatantwort Inhalte aus der Wissensdatenbank verwendet, steht neben dem betreffenden Satz eine Quellenmarkierung mit einem kleinen Dokumentsymbol. Ihre Nummer setzt die gemeinsame Zählung der Datenquellen fort.

- Wählen Sie die Markierung, um unter dem Satz eine Quellenkarte mit Dateiname, Position (Seite, Abschnitt oder Absatz) und Textstelle zu öffnen. Der für die Antwort relevante Teil ist hervorgehoben. Wählen Sie die Markierung oder erneut **×**, um die Karte zu schließen. Bei einer Markierung in einer Tabelle erscheint die Karte unter der Tabelle. Quellenkarten lassen sich öffnen, sobald die Antwort abgeschlossen ist.
- Wählen Sie **In Wissensdatenbank öffnen**, um die Dateidetails mit der zitierten Textstelle zuerst zu öffnen. Wurde die Datei gelöscht oder fehlt Ihnen der Zugriff auf ihren Bereich, weist die Wissensdatenbank darauf hin, dass die Datei nicht geöffnet werden konnte.
- Wurde die Datei erneut verarbeitet und die zitierte Textstelle existiert nicht mehr, meldet die Karte, dass die Quelle aktualisiert wurde. Öffnen Sie die Wissensdatenbank, um den aktuellen Inhalt zu sehen.
- Haben Sie keinen Zugriff auf die Datei, ist die Markierung abgeblendet und weist nur auf eine eingeschränkte Quelle hin, ohne Dateiname oder Inhalt anzuzeigen. Bei einer gelöschten Datei wird angegeben, dass die Quelle nicht mehr verfügbar ist.

## Dateien löschen

Das Löschen in den Dateidetails wird sofort wirksam und kann nicht rückgängig gemacht werden. Wenn Sie mehrere Dateien aus der Liste löschen, können Sie dies innerhalb von fünf Sekunden rückgängig machen. Gelöschtes Material wird nicht mehr in Chats durchsucht.

## Probleme beim Hochladen

Im Upload-Dialog steht, warum eine Datei nicht hochgeladen wurde. Der Dialog bleibt geöffnet und behält den ausgewählten Bereich bei.

- **„Kann nicht zur Wissensdatenbank hinzugefügt werden“**: Das Dateiformat wird nicht unterstützt. Der Dialog listet die unterstützten Formate auf. Sie können die Datei als PDF exportieren und erneut hochladen.
- **„Upload wurde während der Übertragung abgebrochen“**: Die Verbindung wurde während der Übertragung unterbrochen, meist weil das Netzwerkgateway Ihres Unternehmens die Upload-Größe begrenzt. Wenn kleine Dateien funktionieren, große aber wiederholt fehlschlagen, bitten Sie die für die Bereitstellung zuständige Administration, das Limit zu erhöhen. Trat der Fehler nur einmal auf, wählen Sie **Erneut versuchen**, um dieselbe Datei noch einmal hochzuladen.
- **„Netzwerkverbindung fehlgeschlagen“**: Der Upload scheiterte, bevor Daten übertragen wurden. Prüfen Sie Ihre Netzwerkverbindung und wählen Sie **Erneut versuchen**.

Mit **Upload abbrechen** können Sie eine laufende Übertragung stoppen.
