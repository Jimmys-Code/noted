import { useEffect, useRef } from "react";
import { EditorState } from "@codemirror/state";
import { EditorView, keymap, lineNumbers, highlightActiveLine } from "@codemirror/view";
import { defaultKeymap, history, historyKeymap } from "@codemirror/commands";
import { markdown } from "@codemirror/lang-markdown";
import { oneDark } from "@codemirror/theme-one-dark";
import { vim } from "@replit/codemirror-vim";

type Props = {
  value: string;
  onChange: (v: string) => void;
  vimMode: boolean;
};

export default function Editor({ value, onChange, vimMode }: Props) {
  const host = useRef<HTMLDivElement>(null);
  const viewRef = useRef<EditorView | null>(null);
  const onChangeRef = useRef(onChange);
  onChangeRef.current = onChange;

  useEffect(() => {
    if (!host.current) return;
    const state = EditorState.create({
      doc: value,
      extensions: [
        ...(vimMode ? [vim()] : []),
        lineNumbers(),
        highlightActiveLine(),
        history(),
        keymap.of([...defaultKeymap, ...historyKeymap]),
        markdown(),
        oneDark,
        EditorView.lineWrapping,
        EditorView.theme({
          "&": { height: "100%", fontSize: "15px", background: "transparent" },
          ".cm-scroller": {
            fontFamily: "'JetBrains Mono', ui-monospace, monospace",
            lineHeight: "1.65",
          },
          ".cm-gutters": { background: "transparent", border: "none", color: "#444b5c" },
          ".cm-activeLine": { background: "rgba(255,255,255,0.03)" },
          ".cm-activeLineGutter": { background: "transparent", color: "#7c8cff" },
        }),
        EditorView.updateListener.of((u) => {
          if (u.docChanged) onChangeRef.current(u.state.doc.toString());
        }),
      ],
    });
    const view = new EditorView({ state, parent: host.current });
    viewRef.current = view;
    return () => view.destroy();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [vimMode]);

  useEffect(() => {
    const v = viewRef.current;
    if (!v) return;
    const cur = v.state.doc.toString();
    if (cur !== value) {
      v.dispatch({ changes: { from: 0, to: cur.length, insert: value } });
    }
  }, [value]);

  return <div ref={host} className="editor-host" />;
}
