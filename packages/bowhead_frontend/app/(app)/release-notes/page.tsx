import { ReleaseNotesPage } from "@/components/help/release-notes-page";
import { loadReleaseNotes } from "@/lib/help-content";

export const dynamic = "force-static";

export default function ReleaseNotesRoute() {
  return <ReleaseNotesPage article={loadReleaseNotes()} />;
}
