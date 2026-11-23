import {
  CameraIcon,
  HomeIcon,
  KeyboardIcon,
  LockIcon,
  MoonSunIcon,
  StopCircleIcon,
} from "../icons";

interface ActionToolbarProps {
  isLiveAvailable: boolean;
  busyAction: string | null;
  simDarkMode: boolean;
  keyboardOpen: boolean;
  onPressHome: () => void;
  onPressLock: () => void;
  onToggleSimTheme: () => void;
  onSaveScreenshot: () => void;
  onToggleKeyboard: () => void;
  onStopSimulator: () => void;
}

export function ActionToolbar({
  isLiveAvailable,
  busyAction,
  simDarkMode,
  keyboardOpen,
  onPressHome,
  onPressLock,
  onToggleSimTheme,
  onSaveScreenshot,
  onToggleKeyboard,
  onStopSimulator,
}: ActionToolbarProps) {
  const disabled = !isLiveAvailable || busyAction !== null;

  return (
    <div
      role="toolbar"
      aria-label="Simulator hardware and utility controls"
      aria-controls="qa-phone-stage"
      className="stage-toolbar"
    >
      <button
        type="button"
        className="tb-btn"
        disabled={disabled}
        onClick={onPressHome}
        title="Press Home button"
        aria-label="Press Home button"
      >
        <HomeIcon width={15} height={15} />
        <span className="tb-label">Home</span>
      </button>

      <button
        type="button"
        className="tb-btn"
        disabled={disabled}
        onClick={onPressLock}
        title="Press Lock button"
        aria-label="Press Lock button"
      >
        <LockIcon width={15} height={15} />
        <span className="tb-label">Lock</span>
      </button>

      <button
        type="button"
        className="tb-btn"
        disabled={disabled}
        aria-pressed={simDarkMode}
        onClick={onToggleSimTheme}
        title={simDarkMode ? "Switch simulator to Light mode" : "Switch simulator to Dark mode"}
        aria-label="Toggle simulator appearance"
      >
        <MoonSunIcon isDark={simDarkMode} width={15} height={15} />
        <span className="tb-label">{simDarkMode ? "Light" : "Dark"}</span>
      </button>

      <button
        type="button"
        className="tb-btn"
        disabled={disabled}
        onClick={onSaveScreenshot}
        title="Save snapshot screenshot"
        aria-label="Save screenshot"
      >
        <CameraIcon width={15} height={15} />
        <span className="tb-label">{busyAction === "screenshot" ? "Saving…" : "Shot"}</span>
      </button>

      <button
        type="button"
        className="tb-btn"
        disabled={!isLiveAvailable}
        aria-pressed={keyboardOpen}
        onClick={onToggleKeyboard}
        title="Toggle keyboard input bar"
        aria-label="Toggle keyboard input bar"
      >
        <KeyboardIcon width={15} height={15} />
        <span className="tb-label">Keys</span>
      </button>

      <button
        type="button"
        className="tb-btn tb-btn-danger"
        disabled={disabled}
        onClick={onStopSimulator}
        title="Stop this simulator"
        aria-label="Stop simulator"
      >
        <StopCircleIcon width={15} height={15} />
        <span className="tb-label">Stop</span>
      </button>
    </div>
  );
}
