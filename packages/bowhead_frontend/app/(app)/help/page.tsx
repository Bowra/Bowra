import { Suspense } from "react";

import { HelpCenterPage } from "@/components/help/help-center-page";
import { loadHelpArticles } from "@/lib/help-content";

export const dynamic = "force-static";

export default function HelpPage() {
  const articles = loadHelpArticles();

  return (
    <Suspense fallback={null}>
      <HelpCenterPage articles={articles} />
    </Suspense>
  );
}
