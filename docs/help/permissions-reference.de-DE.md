# Berechtigungsübersicht: Rollen und Aktionen

Auf dieser Seite finden Sie schnell heraus, wer für eine Aktion zuständig ist. Team-, Bereichs-, Ressourcen-, Plattform- und Datenberechtigungen werden getrennt geprüft.

## Vier Regeln zum Merken

1. Eine Teamrolle regelt die teamweite Verwaltung. Sie gewährt nicht automatisch die Mitgliedschaft in einem Bereich oder Zugriff auf Geschäftsdaten.
2. Eine Bereichsrolle regelt die Zusammenarbeit in diesem Bereich. Sie macht eine Person nicht zur erstellenden Person aller Agents oder Skills.
3. Zielgruppe und Veröffentlichungsstatus eines Agents bestimmen, wer ihn finden und verwenden darf. Bearbeiten darf ihn weiterhin die erstellende Person oder eine dafür bestimmte Verwaltung.
4. Daten werden mit der Datenidentität des jeweiligen Mitglieds abgefragt. Ein geteilter Bericht oder Agent überträgt diese Identität niemals.

## Wer verwaltet was?

| Objekt oder Aktion                                               | Hauptzuständigkeit                                                  | Hinweise                                                                                                        |
| ---------------------------------------------------------------- | ------------------------------------------------------------------- | --------------------------------------------------------------------------------------------------------------- |
| Teammitgliedschaft, Teamdienste und Freigaben                    | Teamverantwortliche oder Teamadministration                         | Die persönliche Datenidentität anderer Mitglieder wird nicht übertragen.                                        |
| Bereichsmitgliedschaft und Bereichseinstellungen                 | Bereichsadministration                                              | Teamadministratoren treten nicht automatisch jedem Bereich bei.                                                 |
| Inhalte und Versionen eines von einem Mitglied erstellten Agents | Erstellende Person des Agents                                       | Sie wählt die Zielgruppe und veröffentlicht; eine Freigabe kann erforderlich sein.                              |
| Aktivierung und Konfiguration integrierter Agents                | Teamverantwortliche oder Teamadministration                         | Produktdefinierte Tools und Ausgabeformate lassen sich nicht beliebig ersetzen.                                 |
| Persönliche Skill-Bearbeitung und persönliche Aktivierung        | Erstellende Person des Skills oder Mitglied                         | Die persönliche Aktivierung ändert nur die Nutzung durch dieses Mitglied.                                       |
| Freigabe eines Team-Skills oder teamweiter Status                | Teamverantwortliche oder Teamadministration                         | Ein Mitglied kann eine teamweite Deaktivierung nicht außer Kraft setzen.                                        |
| Definitionen von Plattform-Capabilities                          | Produktpflege oder für die Bereitstellung zuständige Administration | Teamrollen konfigurieren unterstützte Funktionen, erstellen aber keine nicht unterstützten Plattformfunktionen. |
| Externe Geschäftsdaten                                           | Administration des Datensystems und Datenidentität des Mitglieds    | Prüfen Sie Datenquelle und persönliche Berechtigung getrennt.                                                   |

## Agent, Plugin, Outcome und Skill

Ein **Agent** bündelt eine Arbeitsrolle und ihre Abhängigkeiten. Ein **Plugin** fasst wiederverwendbare Tools und Skills zusammen; Teamverantwortliche und die Teamadministration verwalten es. Ein **Outcome** beschreibt das zugesagte Ergebnis und kann Nachweise oder Verfügbarkeit anzeigen. Keines davon gewährt Berechtigungen. Ein **Skill** ist eine wiederverwendbare Methode. Einen Entwurf zu speichern, eine Version zu veröffentlichen, eine Teamversion freizugeben und eine Methode für die eigene Sitzung zu aktivieren sind unterschiedliche Aktionen mit unterschiedlichen Zuständigkeiten.

Wenn eine Aktion abgelehnt wird, bestimmen Sie zuerst die genaue Ressource und Aktion. Prüfen Sie dann in dieser Reihenfolge Team, Bereich, Ressource, Capability und Datenzugriff. Den Diagnoseablauf finden Sie unter [Zugriffsprobleme beheben](permissions/troubleshoot-access.de-DE.md).
