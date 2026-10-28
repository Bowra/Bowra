"use client";

import { useEffect } from "react";

import { useAppRouter } from "@/lib/use-app-router";

export default function AdminMembersPage() {
  const router = useAppRouter();
  useEffect(() => router.replace("/team/settings/people/"), [router]);
  return null;
}
