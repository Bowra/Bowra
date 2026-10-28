"use client";

import {
  useEffect,
  useId,
  useLayoutEffect,
  useMemo,
  useRef,
  useState,
  type RefObject,
} from "react";
import { useTranslations } from "next-intl";
import {
  parseInputDocument,
  parseResourceReference,
  type InputDocument,
  type JsonValue,
  type ReferenceNode,
} from "@/lib/composer-input-document";
import {
  completeComposerScope,
  composerDraftScopeSignature,
  type ComposerPersistenceScope,
} from "@/lib/composer-persistence-scope";
import {
  ReferenceParameterSchema,
  type ParameterSchemaNode,
} from "@/lib/reference-parameter-schema";
import {
  initialParameterCells,
  inspectParameterCells,
  isSimpleParameter,
  parameterBaseType,
  type ParameterCell,
} from "@/lib/reference-parameter-form";
import { DialogShell } from "@/components/ui/dialog";
import type { ParameterResourceRef } from "@/lib/reference-catalog-api";
import "@/styles/reference-parameter-panel.css";

export type ReferenceParameterTarget = {
  scope: ComposerPersistenceScope;
  /** A new ID for each explicit opening. A selected tag does not open this panel automatically. */
  editId: string;
  document: InputDocument;
  nodeId: string;
  /** Exact identity associated with the schema, supplied by the metadata owner. */
  resource: ParameterResourceRef;
  parameterSchema: unknown;
};
export type ReferenceParameterIntent = {
  scope: ComposerPersistenceScope;
  editId: string;
  sourceDocument: InputDocument;
  nodeId: string;
  resource: ParameterResourceRef;
  parameters: ReferenceNode["parameters"];
};
type Props = {
  scope: ComposerPersistenceScope | null;
  document: InputDocument | null;
  target: ReferenceParameterTarget | null;
  readOnly?: boolean;
  /** Stable owner when applying replaces the original token DOM node. */
  returnFocusRef?: RefObject<HTMLElement | null>;
  /** Local edit only. The owner applies the captured PM-document command and closes
   * this panel. An intent is not authorization, persistence or task admission. */
  onCommitIntent: (intent: ReferenceParameterIntent) => void;
  onDismiss: (editId: string) => void;
};

function prepare(props: Props) {
  const target = props.target;
  if (
    !target ||
    !target.editId ||
    !completeComposerScope(props.scope) ||
    !completeComposerScope(target.scope) ||
    composerDraftScopeSignature(props.scope) !==
      composerDraftScopeSignature(target.scope)
  )
    return null;
  try {
    const source = parseInputDocument(target.document);
    const current = parseInputDocument(props.document);
    if (JSON.stringify(source) !== JSON.stringify(current)) return null;
    const node = source.nodes.find(
      (item): item is ReferenceNode =>
        item.type === "reference" && item.node_id === target.nodeId,
    );
    const resource = parseResourceReference(target.resource);
    if (
      !node ||
      resource.kind === "agent" ||
      resource.kind === "mcp_source" ||
      Object.entries(resource).some(([key, value]) => Reflect.get(node.resource, key) !== value)
    )
      return null;
    const reader = new ReferenceParameterSchema(target.parameterSchema);
    const schema = reader.snapshot();
    // Identity includes the contract and exact source: metadata refreshes cannot
    // silently reinterpret a field or apply it to a newer draft with the same node ID.
    const key = JSON.stringify([
      composerDraftScopeSignature(props.scope),
      target.editId,
      source,
      node.node_id,
      schema,
    ]);
    return {
      key,
      source,
      node,
      reader,
      schema,
      scope: { ...props.scope },
      editId: target.editId,
    };
  } catch {
    return null;
  }
}

/** Local parameter editing, owned by the scoped Composer and its exact PM transaction. */
export function ReferenceParameterPanel(props: Props) {
  const t = useTranslations("reference_parameters");
  const prepared = useMemo(
    () => prepare(props),
    [props.scope, props.document, props.target],
  );
  const [portalReady, setPortalReady] = useState(false);
  useEffect(() => {
    setPortalReady(true);
  }, []);
  const latest = useRef(props);
  latest.current = props;
  const active = useRef("");
  active.current = prepared?.key ?? "";
  if (!props.target || !portalReady) return null;
  if (!prepared)
    return (
      <div>
        <p role="status">{t("unavailable")}</p>
        <button
          type="button"
          onClick={() => {
            if (latest.current.target === props.target)
              props.onDismiss(props.target!.editId);
          }}
        >
          {t("cancel")}
        </button>
      </div>
    );
  return (
    <ParameterDialog
      key={prepared.key}
      {...props}
      prepared={prepared}
      isCurrent={() => active.current === prepared.key}
    />
  );
}

function ParameterDialog(
  props: Props & {
    prepared: NonNullable<ReturnType<typeof prepare>>;
    isCurrent: () => boolean;
  },
) {
  const t = useTranslations("reference_parameters");
  const { prepared } = props;
  const [cells, setCells] = useState(() =>
    initialParameterCells(prepared.schema, prepared.node.parameters),
  );
  const [committed, setCommitted] = useState(false);
  const lifetime = useRef(false);
  const sent = useRef(false);
  const composing = useRef(false);
  const latest = useRef(props);
  latest.current = props;
  useLayoutEffect(() => {
    lifetime.current = true;
    return () => {
      lifetime.current = false;
    };
  }, []);
  const isCurrent = () => lifetime.current && latest.current.isCurrent();
  const dismiss = () => {
    if (isCurrent()) latest.current.onDismiss(prepared.editId);
  };
  const titleId = useId();
  const helpId = useId();
  const resultId = useId();
  const result = useMemo(() => {
    const result = inspectParameterCells(
      prepared.reader,
      prepared.node.parameters,
      cells,
    );
    if (result.canApply) {
      try {
        parseInputDocument({
          ...prepared.source,
          nodes: prepared.source.nodes.map((node) =>
            node.type === "reference" && node.node_id === prepared.node.node_id
              ? { ...node, parameters: result.values }
              : node,
          ),
        });
      } catch {
        result.canApply = false;
      }
    }
    return result;
  }, [prepared, cells]);
  const disabled = Boolean(props.readOnly || committed);
  const setCell = (name: string, cell: ParameterCell) => {
    if (!isCurrent() || latest.current.readOnly || sent.current) return;
    setCells((value) => new Map(value).set(name, cell));
  };
  const apply = () => {
    if (
      !isCurrent() ||
      latest.current.readOnly ||
      sent.current ||
      composing.current ||
      !result.canApply
    )
      return;
    // Re-parse the complete candidate too: the document's aggregate size and node
    // budgets still apply after defaults or edited parameters are added.
    let sourceDocument: InputDocument;
    let parameters: ReferenceNode["parameters"];
    try {
      sourceDocument = parseInputDocument(prepared.source);
      parameters = prepared.reader.inspect(result.values).values!;
      parseInputDocument({
        ...sourceDocument,
        nodes: sourceDocument.nodes.map((node) =>
          node.type === "reference" && node.node_id === prepared.node.node_id
            ? { ...node, parameters }
            : node,
        ),
      });
    } catch {
      return;
    }
    sent.current = true;
    setCommitted(true);
    latest.current.onCommitIntent({
      scope: { ...prepared.scope },
      editId: prepared.editId,
      sourceDocument,
      nodeId: prepared.node.node_id,
      resource: { ...prepared.node.resource } as ParameterResourceRef,
      parameters,
    });
  };
  const fields = Object.entries(prepared.schema.properties!).sort(
    ([a, sa], [b, sb]) =>
      Number(!isSimpleParameter(sa)) - Number(!isSimpleParameter(sb)) ||
      Number(prepared.schema.required?.includes(b) ?? false) -
        Number(prepared.schema.required?.includes(a) ?? false),
  );
  return (
    <DialogShell
      onClose={dismiss}
      closeOnBackdrop={false}
      // Applying parameters replaces the original token node; on close, return to the stable host supplied by the caller.
      returnFocusRef={props.returnFocusRef}
      labelledBy={titleId}
      backdropClassName="reference-parameter-overlay"
      panelClassName="reference-parameter-panel"
      panelProps={{
        "aria-describedby": helpId,
        onInput: (event) => {
          // Chromium may finish an IME sequence with a non-composing input
          // without compositionend. Reconcile against that native fact too.
          if (event.nativeEvent instanceof InputEvent)
            composing.current = event.nativeEvent.isComposing;
        },
        onCompositionStart: () => {
          composing.current = true;
        },
        onCompositionEnd: () => {
          composing.current = false;
        },
        // Capture phase: during IME composition, Esc/Tab/Enter belong to the IME, not dialog dismissal or focus trapping
        // (DialogShell occupies the panel's onKeyDown, so handle this in capture phase).
        onKeyDownCapture: (event) => {
          if (
            composing.current ||
            event.nativeEvent.isComposing ||
            event.keyCode === 229
          ) {
            event.stopPropagation();
            return;
          }
          // Dismiss here rather than through the shell: floating-ui tracks IME state only
          // from composition events and keeps ignoring Esc when Chromium ends a composition
          // with a plain input and no compositionend. `composing` above already reconciles that.
          if (event.key === "Escape") {
            event.stopPropagation();
            event.preventDefault();
            dismiss();
            return;
          }
          if (event.key === "Enter" && event.target instanceof HTMLInputElement)
            event.preventDefault();
        },
      }}
    >
      <header tabIndex={0} aria-labelledby={titleId}>
        <h2 id={titleId}>{t("title")}</h2>
        <p className="reference-parameter-panel__resource">
          {prepared.node.label}
        </p>
        <p id={helpId}>{t("help")}</p>
      </header>
      <div className="reference-parameter-panel__fields">
        {fields.map(([name, field], index) => (
          <ParameterField
            key={name}
            id={`${titleId}-${index}`}
            name={name}
            schema={field}
            cell={cells.get(name)!}
            disabled={disabled}
            required={prepared.schema.required?.includes(name) ?? false}
            issue={
              result.invalidNumbers.has(name)
                ? "number"
                : result.issues.find(
                    (issue) =>
                      issue.path[0] === name && issue.code !== "required",
                  )?.code
            }
            onChange={(cell) => setCell(name, cell)}
          />
        ))}
        {!fields.length && <p>{t("no_fields")}</p>}
        <p id={resultId} role="status">
          {props.readOnly
            ? t("readonly")
            : committed
              ? t("applied")
              : !result.canApply
                ? t("invalid")
                : result.issues.length
                  ? t("missing")
                  : t("ready")}
        </p>
      </div>
      <footer>
        <button type="button" onClick={dismiss}>
          {t("cancel")}
        </button>
        <button
          type="button"
          className="reference-parameter-panel__apply"
          onClick={apply}
          disabled={disabled || !result.canApply}
          aria-describedby={resultId}
        >
          {t("apply")}
        </button>
      </footer>
    </DialogShell>
  );
}

function ParameterField(props: {
  id: string;
  name: string;
  schema: ParameterSchemaNode;
  cell: ParameterCell;
  disabled: boolean;
  required: boolean;
  issue?: string;
  onChange: (cell: ParameterCell) => void;
}) {
  const t = useTranslations("reference_parameters");
  const { id, name, schema, cell, onChange } = props;
  const base = parameterBaseType(schema);
  const simple = isSimpleParameter(schema);
  const nullable =
    schema.type === "null" ||
    (Array.isArray(schema.type) && schema.type.includes("null"));
  const value = cell.mode === "value" ? cell.value : undefined;
  const choices: JsonValue[] | undefined =
    schema.enum ??
    (base === "boolean"
      ? [true, false, ...(nullable ? [null] : [])]
      : base === "null"
        ? [null]
        : undefined);
  const label = (choice: JsonValue) =>
    choice === null
      ? t("null")
      : choice === true
        ? t("yes")
        : choice === false
          ? t("no")
          : choice === ""
            ? t("empty")
            : String(choice);
  const errorId = `${id}-error`;
  const descriptionId = `${id}-description`;
  const control = {
    id,
    disabled: props.disabled,
    "aria-invalid": Boolean(props.issue),
    "aria-describedby": `${descriptionId}${props.issue ? ` ${errorId}` : ""}`,
  };
  return (
    <section
      className="reference-parameter-field"
      aria-labelledby={`${id}-label`}
    >
      <label id={`${id}-label`} htmlFor={simple ? id : undefined}>
        {schema.title || name}
        {props.required && <span> · {t("required")}</span>}
      </label>
      <div
        id={descriptionId}
        className="reference-parameter-field__description"
      >
        {schema.description && <p>{schema.description}</p>}
        {cell.mode === "value" && cell.defaulted && <p>{t("default")}</p>}
        {!simple && (
          <p>
            {cell.mode === "unset" ? t("complex_missing") : t("complex_kept")}
          </p>
        )}
      </div>
      {simple &&
        (choices ? (
          <select
            {...control}
            value={
              cell.mode === "unset"
                ? "unset"
                : String(choices.findIndex((choice) => choice === value))
            }
            onChange={(event) =>
              onChange(
                event.target.value === "unset"
                  ? { mode: "unset" }
                  : {
                      mode: "value",
                      value: choices[Number(event.target.value)],
                    },
              )
            }
          >
            <option value="unset">{t("later")}</option>
            {cell.mode !== "unset" &&
              !choices.some((choice) => choice === value) && (
                <option value="-1" disabled>
                  {t("invalid_value")}
                </option>
              )}
            {choices.map((choice, index) => (
              <option key={index} value={index}>
                {label(choice)}
              </option>
            ))}
          </select>
        ) : base === "string" && schema.format !== "date" ? (
          <textarea
            {...control}
            rows={2}
            value={typeof value === "string" ? value : ""}
            placeholder={
              value === null
                ? t("null")
                : cell.mode === "unset"
                  ? t("later")
                  : ""
            }
            onChange={(event) =>
              onChange({ mode: "value", value: event.target.value })
            }
          />
        ) : (
          <input
            {...control}
            type={schema.format === "date" ? "date" : "text"}
            inputMode={
              base === "integer" || base === "number" ? "decimal" : undefined
            }
            value={
              cell.mode === "number"
                ? cell.text
                : typeof value === "string" || typeof value === "number"
                  ? String(value)
                  : ""
            }
            onChange={(event) =>
              onChange(
                schema.format === "date"
                  ? event.target.value === ""
                    ? { mode: "unset" }
                    : { mode: "value", value: event.target.value }
                  : { mode: "number", text: event.target.value },
              )
            }
          />
        ))}
      {(!simple || !choices) && (
        <div className="reference-parameter-field__actions">
          <button
            type="button"
            disabled={props.disabled || cell.mode === "unset"}
            onClick={() => onChange({ mode: "unset" })}
          >
            {t("later")}
          </button>
          {nullable && simple && (
            <button
              type="button"
              disabled={props.disabled || value === null}
              onClick={() => onChange({ mode: "value", value: null })}
            >
              {t("null")}
            </button>
          )}
          <span>
            {cell.mode === "unset"
              ? t("unset")
              : value === null
                ? t("null")
                : ""}
          </span>
        </div>
      )}
      {props.issue && (
        <p id={errorId} className="reference-parameter-field__error">
          {t(`error.${props.issue}`)}
        </p>
      )}
    </section>
  );
}
