# Sicherheit und Offline-Sandbox

Codeausführung benötigt eine separat eingerichtete, kompatible Sandbox und einen Executor. Die Community-Quellcodevorschau allein ist keine validierte Laufzeitbereitstellung. Die folgenden Grenzen beschreiben den unterstützten Isolationsvertrag; Betreiber müssen ihn mit der tatsächlich installierten Laufzeit prüfen.

Team-Eigentümer und Administratoren ändern Standard- und Maximallaufzeit unter **Team-Einstellungen › Agents › Laufzeitlimits › Sandbox-Berechnungen**. Die angezeigte Bereitstellungsgrenze darf nicht überschritten werden. Änderungen gelten nach dem Speichern für neue Aufgaben. Mit **Bereitstellungsstandard verwenden**, Prüfung und Speichern übernehmen Sie wieder die Standardwerte. Laufende Aufgaben behalten ihre ursprünglichen Grenzen.

Eine isolierte Berechnung fordert standardmäßig 120 Sekunden an; die voreingestellte Obergrenze der Bereitstellung beträgt 600 Sekunden. Administratoren können beides anpassen. Ein Ausführungs-Timeout bedeutet, dass die angeforderte Zeit abgelaufen ist. Ein nicht erreichbarer Executor weist auf eine fehlgeschlagene Verbindung hin. Ist Antwort oder Bereinigung unbestätigt, bleibt das Ergebnis unbekannt: Prüfen Sie den ursprünglichen Lauf vor einem erneuten Versuch. Mehr Zeit repariert keinen ausgefallenen Dienst.

Bowra führt nicht vertrauenswürdigen, KI-generierten Code für Aufgaben in einer eingeschränkten Ausführungsumgebung aus. Die Sandbox ist eine Schutzschicht, aber nicht die einzige Sicherheitsmaßnahme.

## Ablauf einer isolierten Berechnung

Die Aufgabe erhält ausdrücklich angegebene Eingabedateien und einen begrenzten Befehl. Der Executor erstellt einen isolierten Container, bindet nur die benötigten Arbeitsdaten ein, setzt Laufzeitgrenzen, sammelt erlaubte Ausgaben und zerstört die Umgebung nach Abschluss der Aufgabe. Die Aufgabe erhält weder das Dateisystem des Hosts noch dessen Zugangsdaten oder eine allgemeine Netzwerkverbindung.

## Bedeutung von „Offline-Sandbox“

Die Sandbox hat keine Route ins ausgehende Netzwerk. Der Code kann keine Pakete herunterladen, externe APIs aufrufen, ein internes Netzwerk durchsuchen oder Ausgaben über das Netzwerk herausschleusen. Für unterstützte Aufgaben benötigte Abhängigkeiten sind bereits im Image enthalten. Benötigt eine Aufgabe einen externen Dienst, muss das Produkt diesen über eine ausdrücklich und separat genehmigte Integration bereitstellen und darf den Zugriff nicht über Sandbox-Code herstellen.

## Grenzen der Laufzeitumgebung

- **Identität und Systemaufrufe:** Der Code läuft als Benutzer ohne Root-Rechte und mit eingeschränktem Profil für Systemaufrufe und Berechtigungen.
- **Dateien:** Verfügbar sind nur auf die Aufgabe begrenzte Eingabe- und Ausgabepfade. Host-Einbindungen sind nicht beschreibbar.
- **Ressourcen und Laufzeit:** Grenzen für CPU, Arbeitsspeicher, Prozesse, Ausgaben und Laufzeit stoppen außer Kontrolle geratene Aufgaben. Nach der Ausführung wird die Umgebung entfernt.
- **Executor:** Der Dienst, der Sandboxes erstellt, validiert den Aufgabenvertrag und gibt seine eigenen Zugangsdaten nicht an die Arbeitslast weiter.

## Häufige Angriffsversuche

Netzwerk-Rückrufe scheitern, weil keine Netzwerkroute besteht. Zugriffe auf Host-Dateien, privilegierte Vorgänge und Änderungen an Einbindungen scheitern, weil der Container weder Host-Zugriff noch Linux-Privilegien hat. Fork-Bomben, übermäßige Ausgaben und lang laufende Schleifen werden durch Ressourcenlimits und Zeitüberschreitungen begrenzt. Diese Maßnahmen verringern den möglichen Schaden, machen beliebigen Code aber nicht mathematisch risikofrei.

## Gemeinsame Verantwortung

Nutzerinnen und Nutzer sollten nur die unbedingt erforderlichen Daten bereitstellen und keine Geheimnisse in Aufgabenmaterial einfügen. Teamadministratoren sollten externe Integrationen und Datenquellen einschränken. Für die Bereitstellung zuständige Personen sollten Sandbox-Laufzeit, Executor-Image, Host-Kernel und Container-Richtlinien aktuell halten. Nach jeder relevanten Änderung sollten sie Netzwerkisolation, schreibgeschützte Einbindungen, Ausführung ohne Root-Rechte, entfernte Capabilities und die Bereinigung nach Zeitüberschreitungen überprüfen.

Kein Sicherheitssystem kann garantieren, dass es niemals umgangen wird. Bowra kombiniert Isolation, geringstmögliche Berechtigungen, begrenzte Eingaben, Bereinigung nach der Ausführung und zeitnahe Sicherheitsupdates. Ein Fehler in einer einzelnen Schutzschicht führt dadurch nicht automatisch zu einer Kompromittierung des Hosts oder der Daten.
