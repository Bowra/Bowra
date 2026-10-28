"use client";

import { FeedbackInboxContent } from "@/components/settings/feedback-inbox-content";
import { TeamSettingsRoute } from "@/components/settings/team-settings-route";

export default function TeamFeedbackInboxPage() {
  return <TeamSettingsRoute active="feedback-inbox"><FeedbackInboxContent /></TeamSettingsRoute>;
}
