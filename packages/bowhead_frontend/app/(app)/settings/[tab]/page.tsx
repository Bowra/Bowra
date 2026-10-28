import { PersonalSettingsPageClient } from "./personal-settings-page-client";

const PERSONAL_SETTINGS_TABS = [
  "profile",
  "security",
  "appearance",
  "memory",
  "export",
];

export function generateStaticParams() {
  return PERSONAL_SETTINGS_TABS.map((tab) => ({ tab }));
}

export default function PersonalSettingsPage() {
  return <PersonalSettingsPageClient />;
}
