export const INTERACTIVE_HTML_SANDBOX = "allow-scripts allow-forms";
export const INTERACTIVE_HTML_PERMISSIONS = "camera 'none'; microphone 'none'; geolocation 'none'; display-capture 'none'; clipboard-read 'none'; clipboard-write 'none'; usb 'none'; serial 'none'; hid 'none'; payment 'none'; fullscreen 'none'";
export type InteractiveHtmlDocument = { document: string; channel: string; externalResources: boolean };
export type HtmlPreviewIssue = "runtime" | "blocked";
