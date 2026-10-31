"use client";

import { createContext, useContext, useMemo, type ReactNode } from "react";
import { useLocale, useMessages, useTranslations } from "next-intl";
import { HelpPopover } from "@/components/ui/help-popover";
import { indexContract, parseFieldContract, scalarFacts, type FieldContract, type FieldSchema } from "@/lib/configuration-field-guidance";
import manifest from "@/edition/configuration-field-translations.json";

const Context = createContext<Record<string, FieldSchema>>({});
const entries = manifest.fields as Record<string, { key: string; revision: string }>;
const reviewed = manifest.reviewed as Record<string, Record<string, string>>;

export function ConfigurationFieldProvider({ contracts, children }: { contracts?: Record<string, FieldContract>; children: ReactNode }) {
  const fields = useMemo(
    () =>
      Object.assign(
        {},
        ...Object.values(contracts ?? {}).map((raw) => {
          const contract = parseFieldContract(raw);
          return contract ? indexContract(contract) : {};
        }),
      ),
    [contracts],
  );
  return <Context.Provider value={fields}>{children}</Context.Provider>;
}

/** Read source facts for existing controls; never apply defaults to a draft. */
export function useConfigurationFieldSchema(field: string): FieldSchema | undefined {
  return useContext(Context)[field];
}

/** Explicit control binding. No network, value access, state mutation or generated form controls. */
export function ConfigurationFieldHelp({
  field,
  contract,
  label,
  includeChildren = false,
  facts: contextualFacts,
  childFacts,
}: {
  field: string;
  contract?: FieldContract;
  label?: string;
  includeChildren?: boolean;
  facts?: Pick<FieldSchema, "enum" | "minimum" | "maximum">;
  childFacts?: Record<string, Partial<FieldSchema>>;
}) {
  const inherited = useContext(Context);
  const local = useMemo(() => {
    const parsed = parseFieldContract(contract);
    return parsed ? indexContract(parsed) : {};
  }, [contract]);
  const available = useMemo(() => (Object.keys(local).length ? { ...inherited, ...local } : inherited), [inherited, local]);
  const node = available[field];
  if (!node?.description) return null;
  return (
    <LocalizedFieldHelp
      field={field}
      node={node}
      available={available}
      label={label}
      includeChildren={includeChildren}
      contextualFacts={contextualFacts}
      childFacts={childFacts}
    />
  );
}

function LocalizedFieldHelp({
  field,
  node,
  available,
  label,
  includeChildren,
  contextualFacts,
  childFacts,
}: {
  field: string;
  node: FieldSchema;
  available: Record<string, FieldSchema>;
  label?: string;
  includeChildren: boolean;
  contextualFacts?: Pick<FieldSchema, "enum" | "minimum" | "maximum">;
  childFacts?: Record<string, Partial<FieldSchema>>;
}) {
  const locale = useLocale();
  const messages = useMessages() as { configuration_fields?: Record<string, string> };
  const t = useTranslations("configuration_guidance");
  const entry = entries[field];
  const revision = node["x-bowhead-field"]?.text_revision;
  const current = entry?.revision === revision && (locale === "en-US" || reviewed[locale]?.[field] === revision);
  const description = current ? (messages.configuration_fields?.[entry.key] ?? node.description) : node.description;
  const facts = { ...scalarFacts(node), ...contextualFacts };
  return (
    <span data-configuration-field={field}>
      <HelpPopover preview label={label ? t("about_field", { field: label }) : t("about")} title={label}>
        <p>{description}</p>
        {includeChildren ? (
          <ul>
            {Object.entries(available)
              .filter(([key]) => key.startsWith(`${field}.`))
              .map(([key, child]) => {
                const item = entries[key];
                const rev = child["x-bowhead-field"]?.text_revision;
                const translated = item?.revision === rev && (locale === "en-US" || reviewed[locale]?.[key] === rev);
                return (
                  <li key={key}>
                    {translated ? (messages.configuration_fields?.[item.key] ?? child.description) : child.description}
                    <FieldFacts node={child} facts={{ ...scalarFacts(child), ...childFacts?.[key] }} />
                  </li>
                );
              })}
          </ul>
        ) : null}
        <FieldFacts node={node} facts={facts} />
      </HelpPopover>
    </span>
  );
}

function FieldFacts({ node, facts }: { node: FieldSchema; facts: FieldSchema }) {
  const t = useTranslations("configuration_guidance");
  const types = (Array.isArray(facts.type) ? facts.type : [facts.type]).filter((type): type is string => Boolean(type) && type !== "null");
  const unit = node["x-bowhead-field"]?.unit;
  const minimum = facts.minimum ?? facts.exclusiveMinimum;
  const maximum = facts.maximum;
  return (
    <>
      {types.length ? (
        <p>
          {t("type")}: {types.map((type) => (t.has(`types.${type}`) ? t(`types.${type}`) : type)).join(" / ")}
          {unit && t.has(`units.${unit}`) ? ` · ${t(`units.${unit}`)}` : ""}
        </p>
      ) : null}
      {minimum !== undefined || maximum !== undefined ? (
        <p>
          {t("range")}: {minimum === undefined ? "−∞" : `${facts.exclusiveMinimum !== undefined ? "> " : ""}${minimum}`} – {maximum ?? "∞"}
        </p>
      ) : null}
      {facts.maxLength !== undefined ? <p>{t("max_length", { count: facts.maxLength })}</p> : null}
      {facts.enum ? (
        <p>
          {t("choices")}:{" "}
          {facts.enum
            .filter((v) => v !== null)
            .map(String)
            .join(", ")}
        </p>
      ) : null}
      {node["x-bowhead-field"]?.decimal_places !== undefined ? (
        <p>
          {t("decimal", {
            min: node["x-bowhead-field"]!.decimal_minimum!,
            max: node["x-bowhead-field"]!.decimal_maximum!,
            places: node["x-bowhead-field"]!.decimal_places!,
          })}
        </p>
      ) : null}
    </>
  );
}
