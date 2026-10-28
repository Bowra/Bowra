import StarterKit from "@tiptap/starter-kit";

// Keep the persisted editor schema stable when upgrading to Tiptap 3.
// Links are registered explicitly by each supported editor/parser composition.
export default StarterKit.configure({
  link: false,
  underline: false,
  trailingNode: false,
});
