import { useRef, useState } from "react";
import { BackspaceIcon, ReturnKeyIcon, SendIcon } from "../icons";
import type { PhoneKey } from "../types";

interface KeyboardBarProps {
  disabled: boolean;
  sending: boolean;
  onTypeText: (text: string) => Promise<void>;
  onSendKey: (key: PhoneKey) => Promise<void>;
}

export function KeyboardBar({
  disabled,
  sending,
  onTypeText,
  onSendKey,
}: KeyboardBarProps) {
  const [text, setText] = useState("");
  const inputRef = useRef<HTMLInputElement | null>(null);

  const handleSubmit = async (e: React.FormEvent) => {
    e.preventDefault();
    if (disabled || sending) return;
    if (text.length === 0) {
      await onSendKey("return");
      return;
    }
    const payload = text;
    setText("");
    await onTypeText(payload);
    inputRef.current?.focus();
  };

  return (
    <form
      className="keyboard-bar"
      onSubmit={(e) => void handleSubmit(e)}
      aria-label="Type text or send keys to simulator"
    >
      <div className="keyboard-input-wrap">
        <input
          ref={inputRef}
          type="text"
          className="keyboard-input"
          value={text}
          onChange={(e) => setText(e.target.value)}
          placeholder="Type ASCII or Unicode text to simulator…"
          aria-label="Text to type on simulator"
          enterKeyHint="send"
          disabled={disabled}
          autoComplete="off"
          spellCheck={false}
        />
        <button
          type="submit"
          className="kb-btn kb-btn-primary"
          disabled={disabled || sending || text.length === 0}
          aria-label="Send text"
          title="Send text"
        >
          <SendIcon width={13} height={13} />
          <span className="kb-btn-label">Send</span>
        </button>
      </div>

      <div className="keyboard-quick-keys" role="group" aria-label="Quick keys">
        <button
          type="button"
          className="kb-btn"
          disabled={disabled || sending}
          onClick={() => void onSendKey("return")}
          title="Return"
          aria-label="Return key"
        >
          <ReturnKeyIcon width={13} height={13} />
          <span className="kb-key-text">Ret</span>
        </button>
        <button
          type="button"
          className="kb-btn"
          disabled={disabled || sending}
          onClick={() => void onSendKey("backspace")}
          title="Backspace"
          aria-label="Backspace key"
        >
          <BackspaceIcon width={13} height={13} />
          <span className="kb-key-text">Del</span>
        </button>
        <button
          type="button"
          className="kb-btn"
          disabled={disabled || sending}
          onClick={() => void onSendKey("escape")}
          title="Escape"
          aria-label="Escape key"
        >
          <span>Esc</span>
        </button>
        <button
          type="button"
          className="kb-btn"
          disabled={disabled || sending}
          onClick={() => void onSendKey("tab")}
          title="Tab"
          aria-label="Tab key"
        >
          <span>Tab</span>
        </button>
      </div>
    </form>
  );
}
