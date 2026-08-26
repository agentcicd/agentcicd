import { useCallback, useEffect, useMemo, useState } from "react";

import { cn } from "./service-primitives";

export interface LabelStudioRendererProps {
  config: string;
  task: {
    id: string | number;
    data: Record<string, unknown>;
    annotations?: Array<{
      id?: string | number;
      result: Array<Record<string, unknown>>;
    }>;
  };
  onSubmit?: (annotation: { id?: string | number; result: Array<Record<string, unknown>> }) => void;
  onChange?: (annotation: { id?: string | number; result: Array<Record<string, unknown>> }) => void;
  onSkip?: () => void;
  readOnly?: boolean;
  showActions?: boolean;
}

interface ParsedElement {
  tag: string;
  attributes: Record<string, string>;
  children: ParsedElement[];
  text?: string;
}

interface ParsedTemplate {
  elements: ParsedElement[];
  error?: string;
}

type ChoiceState = Record<string, string[]>;
type TextAreaState = Record<string, string>;

function parseXML(xml: string): ParsedTemplate {
  const parser = new DOMParser();
  const doc = parser.parseFromString(xml, "text/xml");

  function parseNode(node: Element): ParsedElement {
    const attributes: Record<string, string> = {};
    for (const attr of Array.from(node.attributes)) {
      attributes[attr.name.toLowerCase()] = attr.value;
    }

    const children: ParsedElement[] = [];
    let text = "";
    for (const child of Array.from(node.childNodes)) {
      if (child.nodeType === Node.ELEMENT_NODE) {
        children.push(parseNode(child as Element));
      } else if (child.nodeType === Node.TEXT_NODE) {
        text += child.textContent?.trim() || "";
      }
    }

    return { tag: node.tagName.toLowerCase(), attributes, children, text: text || undefined };
  }

  const root = doc.documentElement;
  if (root.tagName === "parsererror") {
    return { elements: [], error: root.textContent?.trim() || "Template XML is invalid." };
  }
  if (root.tagName.toLowerCase() !== "view") {
    return { elements: [], error: "Template XML must use <View> as the root element." };
  }
  return { elements: Array.from(root.children).map((child) => parseNode(child as Element)) };
}

function resolveValue(template: string, data: Record<string, unknown>): string {
  return template.replace(/\$(\w+)/g, (_, key) => {
    const value = data[key];
    return value !== undefined ? renderValue(value) : `$${key}`;
  });
}

function renderValue(value: unknown): string {
  if (value === null || value === undefined) return "";
  if (typeof value === "string") return value;
  if (typeof value === "number" || typeof value === "boolean") return String(value);
  return JSON.stringify(value, null, 2);
}

function isTemplateSubmitControl(element: ParsedElement): boolean {
  const tag = element.tag.replace(/[-_]/g, "").toLowerCase();
  if (tag === "submit" || tag === "submitbutton") return true;
  if (tag !== "button") return false;
  const buttonType = (element.attributes.type || "").trim().toLowerCase();
  if (buttonType === "submit") return true;
  const label = [element.attributes.value, element.attributes.label, element.attributes.text, element.text].filter(Boolean).join(" ");
  return /\b(submit|review|done|complete)\b/i.test(label);
}

export function LabelStudioRenderer({ config, task, onSubmit, onChange, onSkip, readOnly = false, showActions = false }: LabelStudioRendererProps) {
  const [choiceState, setChoiceState] = useState<ChoiceState>({});
  const [textAreaState, setTextAreaState] = useState<TextAreaState>({});
  const parsedTemplate = useMemo(() => parseXML(config), [config]);

  const handleChoiceSelect = useCallback((choicesName: string, value: string, choice: string) => {
    setChoiceState((previous) => {
      const current = previous[choicesName] || [];
      if (choice === "single") return { ...previous, [choicesName]: [value] };
      if (current.includes(value)) return { ...previous, [choicesName]: current.filter((entry) => entry !== value) };
      return { ...previous, [choicesName]: [...current, value] };
    });
  }, []);

  const handleTextChange = useCallback((name: string, value: string) => {
    setTextAreaState((previous) => ({ ...previous, [name]: value }));
  }, []);

  const buildAnnotation = useCallback(() => {
    const result: Array<Record<string, unknown>> = [];
    for (const [name, values] of Object.entries(choiceState)) {
      if (values.length > 0) {
        result.push({ from_name: name, to_name: name, type: "choices", value: { choices: values } });
      }
    }
    for (const [name, value] of Object.entries(textAreaState)) {
      if (value.trim()) {
        result.push({ from_name: name, to_name: name, type: "textarea", value: { text: [value] } });
      }
    }
    return { result };
  }, [choiceState, textAreaState]);

  useEffect(() => {
    onChange?.(buildAnnotation());
  }, [buildAnnotation, onChange]);

  const handleSubmit = useCallback(() => {
    onSubmit?.(buildAnnotation());
  }, [buildAnnotation, onSubmit]);

  const renderElement = (element: ParsedElement, index: number): React.ReactNode => {
    if (isTemplateSubmitControl(element)) return null;
    const { tag, attributes, children, text } = element;
    const key = attributes.name || `${tag}-${index}`;

    switch (tag) {
      case "view":
        return <div key={key} className="space-y-4">{children.map((child, childIndex) => renderElement(child, childIndex))}</div>;
      case "header":
        return <h2 key={key} className="text-lg font-semibold text-slate-900">{resolveValue(text || attributes.value || "", task.data)}</h2>;
      case "text": {
        const textValue = attributes.value ? resolveValue(attributes.value, task.data) : resolveValue(text || "", task.data);
        return <div key={key} className="rounded-lg border border-slate-200 bg-slate-50 p-4 text-slate-800 whitespace-pre-wrap">{textValue}</div>;
      }
      case "section":
        return <section key={key} className="space-y-3 rounded-lg border border-slate-200 p-4">{attributes.title ? <h3 className="text-sm font-semibold text-slate-900">{attributes.title}</h3> : null}{children.map((child, childIndex) => renderElement(child, childIndex))}</section>;
      case "field": {
        const fieldName = attributes.name || "";
        const fieldLabel = attributes.label || fieldName;
        const fieldValue = renderValue(task.data[fieldName]);
        return <div key={key} className="space-y-1">{fieldLabel ? <div className="text-xs font-medium uppercase text-slate-500">{fieldLabel}</div> : null}<div className="rounded-lg border border-slate-200 bg-slate-50 p-3 text-sm text-slate-800 whitespace-pre-wrap">{fieldValue || <span className="text-slate-400">No value</span>}</div></div>;
      }
      case "image": {
        const src = attributes.value ? resolveValue(attributes.value, task.data) : "";
        return <div key={key} className="overflow-hidden rounded-lg border border-slate-200"><img src={src} alt={attributes.name || "Task image"} className="h-auto max-w-full" /></div>;
      }
      case "audio": {
        const src = attributes.value ? resolveValue(attributes.value, task.data) : "";
        return <audio key={key} controls className="w-full"><source src={src} /></audio>;
      }
      case "video": {
        const src = attributes.value ? resolveValue(attributes.value, task.data) : "";
        return <video key={key} controls className="w-full rounded-lg"><source src={src} /></video>;
      }
      case "choices": {
        const choicesName = attributes.name || `choices-${index}`;
        const choiceType = attributes.choice || "single";
        const selected = choiceState[choicesName] || [];
        return <div key={key} className={cn("space-y-2", attributes.showinline === "true" && "flex flex-wrap gap-2 space-y-0")}>{children.map((child, childIndex) => renderElement({ ...child, attributes: { ...child.attributes, _parentname: choicesName, _choicetype: choiceType, _selected: selected.includes(child.attributes.value) ? "true" : "false" } }, childIndex))}</div>;
      }
      case "choice": {
        const parentName = attributes._parentname || "";
        const choiceType = attributes._choicetype || "single";
        const selected = attributes._selected === "true";
        const choiceValue = attributes.value || text || "";
        return <button key={key} type="button" disabled={readOnly} onClick={() => handleChoiceSelect(parentName, choiceValue, choiceType)} className={cn("rounded-lg border px-4 py-2 text-left transition-colors disabled:cursor-default", selected ? "border-slate-500 bg-slate-50 text-slate-700" : "border-slate-200 bg-white hover:bg-slate-50")}>{text || choiceValue}</button>;
      }
      case "radio": {
        const radioName = attributes.name || `radio-${index}`;
        const selected = choiceState[radioName] || [];
        return <div key={key} className="space-y-2">{attributes.label ? <div className="text-sm font-medium text-slate-900">{attributes.label}</div> : null}{children.map((child, childIndex) => renderElement({ ...child, attributes: { ...child.attributes, _parentname: radioName, _choicetype: "single", _selected: selected.includes(child.attributes.value) ? "true" : "false" } }, childIndex))}</div>;
      }
      case "option": {
        const parentName = attributes._parentname || "";
        const choiceType = attributes._choicetype || "single";
        const selected = attributes._selected === "true";
        const optionValue = attributes.value || text || "";
        return <button key={key} type="button" disabled={readOnly} onClick={() => handleChoiceSelect(parentName, optionValue, choiceType)} className={cn("w-full rounded-lg border px-4 py-2 text-left transition-colors disabled:cursor-default", selected ? "border-slate-500 bg-slate-50 text-slate-700" : "border-slate-200 bg-white hover:bg-slate-50")}>{text || optionValue}</button>;
      }
      case "labels": {
        const labelsName = attributes.name || `labels-${index}`;
        const selected = choiceState[labelsName] || [];
        return <div key={key} className="flex flex-wrap gap-2">{children.map((child, childIndex) => {
          const labelValue = child.attributes.value || child.text || "";
          const labelSelected = selected.includes(labelValue);
          const background = child.attributes.background || "#64748b";
          return <button key={`${labelsName}-${childIndex}`} type="button" disabled={readOnly} onClick={() => handleChoiceSelect(labelsName, labelValue, "multiple")} className={cn("rounded-full px-3 py-1 text-sm font-medium transition-all", labelSelected && "ring-2 ring-slate-500 ring-offset-2")} style={{ backgroundColor: labelSelected ? background : "#e5e7eb", color: labelSelected ? "#fff" : "#374151" }}>{child.text || labelValue}</button>;
        })}</div>;
      }
      case "label":
        return null;
      case "textarea": {
        const textAreaName = attributes.name || `textarea-${index}`;
        return <textarea key={key} placeholder={attributes.placeholder || ""} value={textAreaState[textAreaName] || ""} onChange={(event) => handleTextChange(textAreaName, event.target.value)} readOnly={readOnly} rows={attributes.rows ? parseInt(attributes.rows, 10) : 4} className="w-full rounded-md border border-slate-300 bg-white px-3 py-2 text-sm text-slate-800" />;
      }
      case "rating": {
        const ratingName = attributes.name || `rating-${index}`;
        const maxRating = parseInt(attributes.maxrating || "5", 10);
        const ratingValue = choiceState[ratingName]?.[0];
        return <div key={key} className="flex flex-wrap gap-1">{Array.from({ length: maxRating }, (_, ratingIndex) => {
          const value = String(ratingIndex + 1);
          const active = ratingValue === value;
          return <button key={value} type="button" disabled={readOnly} onClick={() => handleChoiceSelect(ratingName, value, "single")} className={cn("h-10 w-10 rounded-full border-2 text-sm font-medium transition-colors", active ? "border-yellow-500 bg-yellow-400 text-white" : "border-slate-300 bg-white hover:border-yellow-400")}>{value}</button>;
        })}</div>;
      }
      default:
        if (children.length > 0) return <div key={key} className="space-y-2">{children.map((child, childIndex) => renderElement(child, childIndex))}</div>;
        if (text) return <p key={key} className="text-slate-700">{resolveValue(text, task.data)}</p>;
        return null;
    }
  };

  if (parsedTemplate.error) {
    return <div className="rounded-lg border border-red-200 bg-red-50 p-4 text-sm text-red-800"><div className="font-medium">Invalid Label Studio template XML</div><div className="mt-2 whitespace-pre-wrap">{parsedTemplate.error}</div></div>;
  }

  return <div className="space-y-6"><div className="space-y-4">{parsedTemplate.elements.map((element, index) => renderElement(element, index))}</div>{!readOnly && showActions ? <div className="flex items-center gap-3 border-t border-slate-200 pt-4"><button type="button" className="rounded-md bg-slate-900 px-3 py-2 text-sm font-medium text-white hover:bg-slate-700" onClick={handleSubmit}>Submit</button>{onSkip ? <button type="button" className="rounded-md border border-slate-300 bg-white px-3 py-2 text-sm font-medium text-slate-800 hover:bg-slate-50" onClick={onSkip}>Skip</button> : null}</div> : null}</div>;
}

export default LabelStudioRenderer;
