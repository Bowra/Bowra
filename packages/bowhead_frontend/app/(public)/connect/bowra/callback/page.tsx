import type { Metadata } from "next";

import { BowraCallback } from "@/components/bowra-callback";

export const metadata: Metadata = { referrer: "no-referrer" };

export default function BowraCallbackPage() {
  return <BowraCallback />;
}
