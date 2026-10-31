# Agent-Capabilities konfigurieren

Nutzen Sie diese Anleitung, wenn ein Team einen integrierten Subagent aktivieren oder sein unterstütztes Verhalten konfigurieren muss. Von Mitgliedern erstellte Agents werden von den jeweiligen Autorinnen und Autoren gepflegt und veröffentlicht.

## Mit KI einen textbasierten Assistenten entwerfen

Wenn die KI-gestützte Entwurfsprüfung verfügbar ist, beschreiben Sie, dass der Assistent ausschließlich mit von Ihnen bereitgestelltem Text arbeiten soll, ohne Datenabfragen oder Webzugriff.

1. Prüfen Sie Zweck und Anweisungen.
2. Stellen Sie sicher, dass keine Tools oder Skills benötigt werden, und bestätigen Sie, dass nur bereitgestellter Text verwendet werden soll.
3. Wählen Sie **Agent erstellen**. Wenn Sie den Entwurf bearbeiten, speichern und prüfen Sie ihn und bestätigen Sie anschließend erneut.

Benötigt die Aufgabe Datenabfragen, Dateien oder Skripte und ist kein passendes Tool verfügbar, richten Sie die erforderlichen Ressourcen ein oder verwenden Sie die manuelle Konfiguration. Eine leere Toolliste ermöglicht solche Aufgaben nicht. Ist der KI-Entwurf für Textaufgaben nicht verfügbar, folgen Sie den manuellen Möglichkeiten auf Ihrer Seite.

## Assistenten erstellen, der nur bereitgestellten Text verwendet

Wenn die manuelle Erstellung erlaubt, Tools und Skills leer zu lassen, können Sie einen Assistenten für Zusammenfassungen, Übersetzungen oder Textbearbeitung erstellen:

1. Geben Sie einen Namen, eine Routing-Beschreibung und Arbeitsanweisungen ein, die den Assistenten auf bereitgestellte Inhalte begrenzen.
2. Lassen Sie die Toolauswahl leer und fahren Sie mit der Konfiguration fort. Auch Skills können leer bleiben.
3. Prüfen Sie die Konfiguration und wählen Sie **Agent erstellen**. Neue Agents sind standardmäßig privat.

Wenn der Hinweis nicht verfügbar ist, richten Sie sich nach den Anforderungen auf Ihrer Seite. Ohne konfigurierte Tools kann der Assistent keine Daten abfragen, auf externe Dienste zugreifen oder Skripte ausführen. Fügen Sie autorisierte Ressourcen hinzu, wenn solche Funktionen erforderlich sind. Änderungen an bestehenden Agents laufen über den Speichern-und-Veröffentlichen-Ablauf der jeweiligen Verwaltungsseite.

## Integrierten Subagent aktivieren oder deaktivieren

Teamverantwortliche und Teamadministratoren öffnen **Agents → Integrierte Agents verwalten** und verwenden den Aktivierungsschalter der gewünschten Zeile. Lesen Sie gegebenenfalls die Auswirkungen vor der Bestätigung. Warten Sie, bis die Zeile den gewünschten Status anzeigt. **Systemvorgaben** und **Angepasst** zeigen, ob Teamänderungen vorliegen.

Die Liste enthält auch **Coach** und den **Konfigurationsagent**. Bei diesen beiden können Sie nur aktivieren oder deaktivieren sowie Namen und Beschreibung ändern. Die übrigen Felder blendet der Dialog aus, weil Modell, Skills und Anweisungen vom Produkt festgelegt sind. Ein deaktivierter integrierter Agent erscheint nicht mehr im Agent-Marktplatz und in der @-Liste im Chat und erhält keine neuen Aufgaben. Wenn Sie ein Feld ändern, das ein Agent nicht zulässt, schlägt das Speichern fehl und die Meldung nennt das Feld.

## Laufzeitlimits festlegen

Beim Erstellen und Konfigurieren von Agents gelten diese Teamlimits; es gibt kein separates Schrittlimit pro Agent.

Team-Inhaber und Team-Admins legen unter **Team-Einstellungen › Agents › Laufzeitlimits** Limits für alle Agents des Teams fest, integrierte und eigene:

- **Zeitlimit pro Runde**: wie lange eine Gesprächsrunde laufen darf. Standard sind 30 Minuten; möglich sind 10 Minuten bis 10 Stunden. Agents halten 5 Minuten früher an, damit die Antwort geschrieben werden kann.
- **Zeit pro Modellaufruf**: folgt standardmäßig der verbleibenden Agent-Laufzeit. Mit **Höchstens** können Sie eine kürzere Grenze von 30–36.000 Sekunden setzen. Das verlängert die Runde nicht; das Timeout der Modellverbindung gilt weiterhin.
- **Schrittlimit pro Aufgabe**: wie viele Schritte ein Agent für eine Aufgabe machen darf. Standard ist **Unbegrenzt**. Ein Agent, der mehrere Schritte lang keine neuen Ergebnisse liefert, wird automatisch angehalten; ein Schrittlimit ist daher meist nicht nötig.
- **Tool-Aufrufe pro Runde**: Gesamtzahl der Tool-Aufrufe aller Agents in einer Runde. Standard sind 300; jedes Tool zählt gleich.

1. Öffnen Sie **Team-Einstellungen › Agents** und wählen Sie den Tab **Laufzeitlimits**.
2. Ändern Sie die gewünschten Werte. Liegt ein Wert außerhalb des Bereichs, steht der erlaubte Bereich unter dem Feld und Speichern ist nicht verfügbar.
3. Wählen Sie **Speichern**. Die Limits gelten ab der nächsten Runde; laufende Runden sind nicht betroffen. Mit **Standard wiederherstellen** kehren Sie zu den Standardwerten zurück.

Wird ein Limit erreicht, hält der Agent an und berichtet, was fertig ist und was noch fehlt; im Gespräch ist er als **Teilweise abgeschlossen** markiert. Antworten Sie mit „weiter“, damit er fortfährt. Höhere Limits ermöglichen mehr pro Aufgabe, bedeuten aber längere Wartezeiten und mehr Verbrauch.

## Sandbox- und HTML-Grenzen festlegen

Unter **Team-Einstellungen › Agents › Laufzeitlimits** können Team-Eigentümer und Administratoren zusätzlich Folgendes einstellen:

- **Sandbox-Berechnungen**: Standardlaufzeit (anfangs 120 Sekunden) und maximale Laufzeit (anfangs 600 Sekunden). Die Standardlaufzeit darf das Maximum nicht überschreiten. Die angezeigte Bereitstellungsgrenze kann nur die für den Betrieb zuständige Person erhöhen.
- **HTML-Berichte**: maximale Größe jeder eingebetteten HTML-Datei einschließlich eingebetteter Bilder und Daten. Anfangs 25 MiB, höchstens 50 MiB. Größere Dateien und komplexe Tabellen benötigen mehr Browserspeicher und Verarbeitungszeit. Gespeicherte Teamwerte und Bereitstellungsstandards gelten weiterhin. Erhöhen und speichern Sie dieses Feld, wenn ein älteres Limit die Vorschau verhindert. Diese Einstellung begrenzt die Datei, nicht die Token-Ausgabe des Modells.

Die Seite zeigt Standardwerte und zulässige Bereiche. Wählen Sie nach der Bearbeitung **Sandbox- und HTML-Grenzen speichern**. Mit **Bereitstellungsstandard verwenden** übernehmen Sie die Standardwerte zunächst in den Entwurf; prüfen und speichern Sie sie anschließend. Aufgabenlaufzeit und Sandbox/HTML werden separat gespeichert. Bei gleichzeitigen Änderungen bleiben Ihre Eingaben erhalten; verwerfen und laden Sie die aktuellen Einstellungen vor der erneuten Bearbeitung.

Sandbox-Änderungen gelten für neue Aufgaben. Laufende Aufgaben behalten ihre anfänglichen Grenzen. HTML-Vorschauen verwenden beim nächsten Laden die Einstellung des Teams des Berichts. Mehr Zeit hilft langsamen Berechnungen, behebt aber keinen ausgefallenen Dienst. Prüfen Sie unbekannte Ausführungsergebnisse vor einem erneuten Versuch.

## Zuständigkeit und automatisch verwendete Skills konfigurieren

1. Wählen Sie beim integrierten Subagent **Konfigurieren**. Die Einstellungen sind in **Name und Zweck**, **Ausführung** und **Teamvorgaben** gegliedert; die Aktionen bleiben beim Scrollen sichtbar.
2. Ändern Sie Anzeigenamen, Routing-Beschreibung oder Teamhinweise, sofern unterstützt.
3. Wählen Sie eine verfügbare Hauptzuständigkeit und die Skills, die der Agent automatisch berücksichtigen soll. Schritte, Zeit und Tool-Aufrufe legen Sie für alle Agents im Tab **Laufzeitlimits** fest; siehe den nächsten Abschnitt.
4. Speichern Sie, öffnen Sie die Konfiguration erneut und prüfen Sie die Werte.
5. Testen Sie eine passende Aufgabe mit einem Konto, das Zugriff auf den Zielbereich und die Daten hat.

Zuständigkeit und Skills helfen dem System bei der Auswahl eines Agents. Sie gewähren Mitgliedern keine Datenberechtigungen und stellen nicht sicher, dass ein externes Tool konfiguriert ist. Produktdefinierte Tools, Ausgabeformate und Aufrufe anderer Agents können in dieser Konfiguration nicht beliebig ersetzt werden.

## Zuständige Stelle für weitere Freigaben finden

| Bedarf                                                             | Zuständige Stelle                                                                                                                                     |
| ------------------------------------------------------------------ | ----------------------------------------------------------------------------------------------------------------------------------------------------- |
| Teammodell, Such- oder Bilddienst                                  | Teamverantwortliche oder Teamadministration unter **Team-Einstellungen → Modelle**                                                                    |
| Externe Toolquelle oder Verbindung                                 | Teamverantwortliche oder Teamadministration unter **Team-Einstellungen → Tools**                                                                      |
| Datarc-Verbindung                                                  | Teamverantwortliche oder Teamadministration unter **Team-Einstellungen → Datenquellen**; Mitglieder benötigen weiterhin eine eigene Datenberechtigung |
| Anweisungen oder Skills eines von einem Mitglied erstellten Agents | Erstellende Person dieses Agents                                                                                                                      |
| Eine vom Produkt nicht angebotene Plattformfunktion                | Zuständige Produktpflege                                                                                                                              |

Ein Outcome beschreibt das zugesagte Ergebnis eines Agents und kann Nachweise oder Verfügbarkeit anzeigen. Ein Team-Plugin bündelt Tools und Skills für die Auswahl durch Agent-Autoren. Beides ersetzt keine Berechtigung für Funktionen, Tools oder Daten.
