"use client";

/**
 * Knowledge bases (FEATURE_KNOWLEDGE_BASES, PRD §4.4 ⑤): New knowledge base.
 *
 * Name (required, at most 120 characters), description (optional, at most 1000; the assistant reads it to decide
 * when to search), where it lives (My / the current Space / the Team) and who can use it. Homes the caller may
 * not create in stay visible and disabled with the reason (PRD §3: Space viewers cannot create Space knowledge
 * bases; Team knowledge bases need the Team owner or an administrator). The audience depends on the home: My is
 * always "only me"; a Space knowledge base is "members only" or "all Space members" with a default level of
 * "can use" (Q3) or "can edit"; a Team knowledge base is "members only" or "everyone in the Team can use".
 * Bottom sheet on phones.
 */

import { useId, useState, type FormEvent } from "react";
import { useTranslations } from "next-intl";

import { Button } from "@/components/ui/button";
import { Dialog } from "@/components/ui/dialog";
import { Field, fieldDescribedBy } from "@/components/ui/field";
import { LoadingSkeleton } from "@/components/ui/loading-skeleton";
import { Notice } from "@/components/ui/notice";
import { Radio, RadioGroup } from "@/components/ui/radio-group";
import {
  createKnowledgeBase,
  notifyKnowledgeBasesChanged,
  type KnowledgeBaseAudience,
  type KnowledgeBaseDTO,
  type KnowledgeBaseHome,
} from "@/lib/knowledge-bases-api";
import { useSpaceSelection } from "@/lib/use-space";
import { notify } from "@/lib/use-toast";

import {
  KNOWLEDGE_BASE_DESCRIPTION_MAX,
  KNOWLEDGE_BASE_NAME_MAX,
  knowledgeBaseErrorText,
  textLength,
} from "./kb-directory-shared";
import { useKnowledgeBaseDirectory } from "./use-knowledge-bases";

/** The card order of ⑤. */
const HOMES: readonly KnowledgeBaseHome[] = ["personal", "space", "team"];
/** The home preselected when allowed (the mockup and the upload default both favour the current Space). */
const PREFERRED: readonly KnowledgeBaseHome[] = ["space", "personal", "team"];

type AudienceChoice = "members" | "open";

/** The audience the API takes for a home and a choice ("open" = all Space members / everyone in the Team). */
export function audienceFor(home: KnowledgeBaseHome, choice: AudienceChoice): KnowledgeBaseAudience {
  if (home === "personal") return "owner";
  if (choice === "members") return "members";
  return home === "space" ? "space" : "team";
}

export function KbCreateDialog({
  onClose,
  onCreated,
}: {
  onClose: () => void;
  onCreated: (kb: KnowledgeBaseDTO) => void;
}) {
  const t = useTranslations("knowledge_bases.create");
  const tc = useTranslations("knowledge_bases.common");
  const tDir = useTranslations("knowledge_bases.directory");
  const tKb = useTranslations("knowledge_base");
  const { directory, loading, error: loadError, refresh } = useKnowledgeBaseDirectory();
  const { activeId, items: spaces } = useSpaceSelection();
  const formId = useId();
  const nameId = `${formId}-name`;
  const descriptionId = `${formId}-description`;
  const homeLabelId = `${formId}-home`;
  const audienceLabelId = `${formId}-audience`;
  const levelLabelId = `${formId}-level`;

  const [name, setName] = useState("");
  const [description, setDescription] = useState("");
  const [picked, setPicked] = useState<KnowledgeBaseHome | null>(null);
  const [audienceChoice, setAudienceChoice] = useState<AudienceChoice>("members");
  const [level, setLevel] = useState<"use" | "edit">("use");
  const [submitted, setSubmitted] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const spaceId = directory ? directory.space_id : activeId;
  const space = spaces.find((item) => item.id === spaceId) ?? null;
  const can = directory?.can_create ?? null;

  const reasonFor = (home: KnowledgeBaseHome): string | null => {
    if (!can || can[home]) return null;
    if (home === "personal") return t("reason_personal");
    if (home === "team") return t("reason_team");
    if (!directory?.space_id) return t("reason_no_space");
    return space?.role === "viewer" ? t("reason_space_viewer") : t("reason_space_other");
  };
  const allowedDefault = can ? (PREFERRED.find((home) => can[home]) ?? null) : null;
  const home: KnowledgeBaseHome | null = picked && can?.[picked] ? picked : allowedDefault;

  const trimmedName = name.trim();
  const nameLength = textLength(trimmedName);
  const descriptionLength = textLength(description.trim());
  const nameError = submitted && nameLength === 0
    ? t("name_required")
    : nameLength > KNOWLEDGE_BASE_NAME_MAX ? t("name_too_long", { max: KNOWLEDGE_BASE_NAME_MAX, n: nameLength }) : null;
  const descriptionError = descriptionLength > KNOWLEDGE_BASE_DESCRIPTION_MAX
    ? t("description_too_long", { max: KNOWLEDGE_BASE_DESCRIPTION_MAX, n: descriptionLength })
    : null;
  const ready = home !== null && nameLength > 0 && !nameError && !descriptionError;

  const submit = async (event?: FormEvent) => {
    event?.preventDefault();
    setSubmitted(true);
    if (busy || !ready || !home) return;
    setBusy(true);
    setError(null);
    const audience = audienceFor(home, audienceChoice);
    try {
      const kb = await createKnowledgeBase({
        name: trimmedName,
        description: description.trim() || null,
        home,
        audience,
        ...(audience === "space" ? { audience_level: level } : {}),
      });
      notifyKnowledgeBasesChanged();
      notify("success", t("created_toast", { name: kb.name ?? trimmedName }));
      onCreated(kb);
    } catch (e) {
      setError(knowledgeBaseErrorText(e, tKb, t("failed")));
      // A refusal may mean the creation rights changed (for example a new Space role): read them again.
      refresh();
      setBusy(false);
    }
  };

  const homeTitle = (value: KnowledgeBaseHome) => {
    if (value === "personal") return t("home_personal_title");
    if (value === "team") return t("home_team_title");
    return space?.name ? t("home_space_title", { name: space.name }) : t("home_space_title_none");
  };
  const homeDesc = (value: KnowledgeBaseHome) => t(`home_${value}_desc`);

  const audienceHint = !home
    ? null
    : home === "personal"
      ? null
      : audienceChoice === "members"
        ? t(home === "space" ? "hint_members_space" : "hint_members_team")
        : home === "team"
          ? t("hint_team")
          : t(level === "edit" ? "hint_space_edit" : "hint_space_use");

  return (
    <Dialog
      title={t("title")}
      onClose={onClose}
      busy={busy}
      width={560}
      presentation="sheet"
      footer={(
        <>
          <Button onClick={onClose} disabled={busy}>
            {tc("cancel")}
          </Button>
          <Button
            variant="primary"
            type="submit"
            form={formId}
            loading={busy}
            disabled={busy || home === null}
            data-testid="kb-create-submit"
          >
            {busy ? t("submitting") : t("submit")}
          </Button>
        </>
      )}
    >
      <form id={formId} className="kbd-create" onSubmit={(event) => void submit(event)} noValidate>
        <Field id={nameId} label={t("name_label")} error={nameError}>
          <input
            id={nameId}
            className="settings-input ui-input"
            value={name}
            autoComplete="off"
            placeholder={t("name_placeholder")}
            aria-invalid={nameError ? true : undefined}
            aria-required
            aria-describedby={fieldDescribedBy(nameId, { error: Boolean(nameError) })}
            disabled={busy}
            onChange={(event) => setName(event.target.value)}
          />
        </Field>
        <Field id={descriptionId} label={t("description_label")} hint={t("description_hint")} error={descriptionError}>
          <textarea
            id={descriptionId}
            className="settings-textarea"
            rows={3}
            value={description}
            placeholder={t("description_placeholder")}
            aria-invalid={descriptionError ? true : undefined}
            aria-describedby={fieldDescribedBy(descriptionId, { hint: true, error: Boolean(descriptionError) })}
            disabled={busy}
            onChange={(event) => setDescription(event.target.value)}
          />
        </Field>
        <div className="settings-field">
          <span id={homeLabelId} className="settings-field__label">{t("home_label")}</span>
          {!can ? (
            loadError && !loading ? (
              <Notice tone="error">
                <span>{knowledgeBaseErrorText(loadError, tKb, tDir("load_failed_generic"))}</span>{" "}
                <Button size="sm" onClick={refresh}>{tc("retry")}</Button>
              </Notice>
            ) : (
              <LoadingSkeleton rows={3} label={t("loading")} />
            )
          ) : (
            <RadioGroup<KnowledgeBaseHome>
              orientation="both"
              className="kbd-home-cards"
              aria-labelledby={homeLabelId}
              value={home}
              busy={busy}
              onValueChange={(value) => {
                if (!can[value]) return;
                setPicked(value);
                setError(null);
              }}
            >
              {HOMES.map((value) => {
                const reason = reasonFor(value);
                return (
                  <Radio
                    key={value}
                    value={value}
                    disabled={Boolean(reason)}
                    className="kbd-home-card"
                    data-testid={`kb-create-home-${value}`}
                  >
                    <span className="kbd-home-card__title">{homeTitle(value)}</span>
                    <span className={reason ? "kbd-home-card__reason" : "kbd-home-card__desc"}>
                      {reason ?? homeDesc(value)}
                    </span>
                  </Radio>
                );
              })}
            </RadioGroup>
          )}
        </div>
        {home ? (
          <div className="settings-field" data-testid="kb-create-audience">
            <span id={audienceLabelId} className="settings-field__label">{t("audience_label")}</span>
            {home === "personal" ? (
              <p className="kbd-create__fixed" data-testid="kb-create-audience-fixed">{t("audience_owner_fixed")}</p>
            ) : (
              <>
                <RadioGroup<AudienceChoice>
                  appearance="segmented"
                  aria-labelledby={audienceLabelId}
                  value={audienceChoice}
                  busy={busy}
                  onValueChange={setAudienceChoice}
                >
                  <Radio value="members" data-testid="kb-create-audience-members">{t("audience_members")}</Radio>
                  <Radio value="open" data-testid="kb-create-audience-open">
                    {home === "space" ? t("audience_space") : t("audience_team")}
                  </Radio>
                </RadioGroup>
                {home === "space" && audienceChoice === "open" ? (
                  <div className="kbd-create__level">
                    <span id={levelLabelId} className="kbd-create__level-label">{t("level_label")}</span>
                    <RadioGroup<"use" | "edit">
                      appearance="segmented"
                      aria-labelledby={levelLabelId}
                      value={level}
                      busy={busy}
                      onValueChange={setLevel}
                    >
                      <Radio value="use" data-testid="kb-create-level-use">{tc("level_use")}</Radio>
                      <Radio value="edit" data-testid="kb-create-level-edit">{tc("level_edit")}</Radio>
                    </RadioGroup>
                  </div>
                ) : null}
                {audienceHint ? <p className="settings-field__hint">{audienceHint}</p> : null}
              </>
            )}
          </div>
        ) : null}
        {error ? <Notice tone="error">{error}</Notice> : null}
      </form>
    </Dialog>
  );
}
