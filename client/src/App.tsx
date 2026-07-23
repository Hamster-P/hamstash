import { useEffect, useRef, useState } from "react";
import Sidebar, { type View } from "./components/Sidebar";
import TrackingPage from "./pages/TrackingPage";
import SearchPage from "./pages/SearchPage";
import LibraryPage from "./pages/LibraryPage";
import DownloadPage from "./pages/DownloadPage";
import DownloadManagerPage from "./pages/DownloadManagerPage";
import RssPage from "./pages/RssPage";
import SettingsPage, { type SettingsPageHandle } from "./pages/SettingsPage";
import DetailPage from "./pages/DetailPage";
import QbittorrentSetupPage from "./pages/QbittorrentSetupPage";
import { ThemeProvider } from "./theme/ThemeContext";
import "./index.css";

const API_BASE = "http://127.0.0.1:8080";

interface DownloadPrefill {
  keyword: string;
  bgmId: number | null;
  subscribe: boolean;
}

function App() {
  return (
    <ThemeProvider>
      <AppContent />
    </ThemeProvider>
  );
}

function AppContent() {
  const [view, setView] = useState<View>("tracking");
  const [selectedBgmId, setSelectedBgmId] = useState<number | null>(null);
  const [downloadPrefill, setDownloadPrefill] = useState<DownloadPrefill | null>(
    null,
  );
  // 媒体库"手动指定动漫"流程当前正在绑定的文件夹名,非空时搜索页/详情页进入绑定模式
  const [manualMatchFolder, setManualMatchFolder] = useState<string | null>(null);
  // 进详情页那一刻的view,详情页"返回"要回到这里——不能直接展示当前view,
  // 因为详情页点"下载"会把view改成"download",如果从下载页再退回详情页、
  // 又点了详情页自己的"返回",这时候view还停在"download",会显示成下载页而不是
  // 一开始进详情页之前所在的那个tab(比如追更),导致详情页/下载页来回横跳出不去。
  const [detailReturnView, setDetailReturnView] = useState<View | null>(null);
  // 首次引导:没配好qBittorrent连接之前,阻断整个应用只显示引导向导。
  const [qbitSetupCompleted, setQbitSetupCompleted] = useState<boolean | null>(null);

  const settingsRef = useRef<SettingsPageHandle>(null);
  // 从设置页切走时如果有未保存的修改,先暂存目标tab,弹确认框问用户
  const [pendingView, setPendingView] = useState<View | null>(null);

  useEffect(() => {
    fetch(`${API_BASE}/settings`)
      .then((res) => res.json())
      .then((data) => setQbitSetupCompleted(Boolean(data.qbit_setup_completed)))
      .catch(() => setQbitSetupCompleted(false));
  }, []);

  const goToView = (v: View) => {
    setSelectedBgmId(null);
    setManualMatchFolder(null);
    setDetailReturnView(null);
    setView(v);
  };

  // 从追更/搜索/影视库点进详情页:记住当时所在的tab,详情页"返回"要回到这里
  const handleSelectAnime = (bgmId: number) => {
    setDetailReturnView(view);
    setSelectedBgmId(bgmId);
  };

  // 详情页自己的"返回"按钮:回到进详情页之前所在的tab,而不是当前view
  // (当前view可能已经被"下载"按钮那趟跳转改成"download"了)
  const handleBackFromDetail = () => {
    setSelectedBgmId(null);
    if (detailReturnView) {
      setView(detailReturnView);
      setDetailReturnView(null);
    }
  };

  // 切换主导航时,顺手清空详情页状态,避免切到别的tab还停留在详情页;
  // 如果当前在设置页且有未保存的修改,先拦截,等用户确认后再真正切换
  const handleViewChange = (v: View) => {
    if (pendingView !== null) return;
    if (view === "settings" && v !== "settings" && settingsRef.current?.isDirty()) {
      setPendingView(v);
      return;
    }
    goToView(v);
  };

  const handleDiscardAndLeaveSettings = () => {
    if (pendingView) goToView(pendingView);
    setPendingView(null);
  };

  const handleSaveAndLeaveSettings = async () => {
    await settingsRef.current?.save();
    if (pendingView) goToView(pendingView);
    setPendingView(null);
  };

  const handleCancelLeaveSettings = () => setPendingView(null);

  // 从详情页点"下载"或"RSS订阅"按钮时,带着这部番的信息跳到下载页
  const handleNavigateToDownload = (
    keyword: string,
    bgmId: number | null,
    subscribe: boolean,
  ) => {
    setDownloadPrefill({ keyword, bgmId, subscribe });
    setSelectedBgmId(null);
    setView("download");
  };

  // 从媒体库点"指定动漫"时,带着文件夹名跳到搜索页,进入手动绑定模式
  const handleManualMatch = (folderName: string) => {
    setManualMatchFolder(folderName);
    setSelectedBgmId(null);
    setDetailReturnView(null);
    setView("search");
  };

  // 在详情页确认匹配后,把 bgm_id 绑定到该文件夹并回到媒体库
  const handleConfirmMatch = async (bgmId: number) => {
    if (!manualMatchFolder) return;
    await fetch(`${API_BASE}/library/match`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ folder_name: manualMatchFolder, bgm_id: bgmId }),
    }).catch((err: any) => console.error("绑定动漫失败", err));
    setManualMatchFolder(null);
    setSelectedBgmId(null);
    setDetailReturnView(null);
    setView("library");
  };

  if (qbitSetupCompleted === null) {
    return (
      <div className="flex h-screen w-screen items-center justify-center bg-ink font-mono text-xs text-muted">
        加载中...
      </div>
    );
  }

  if (!qbitSetupCompleted) {
    return <QbittorrentSetupPage onComplete={() => setQbitSetupCompleted(true)} />;
  }

  return (
    <div className="flex h-screen w-screen overflow-hidden">
      <Sidebar active={view} onChange={handleViewChange} />
      <main className="flex-1 overflow-y-auto">
        {selectedBgmId !== null ? (
          <DetailPage
            bgmId={selectedBgmId}
            onBack={handleBackFromDetail}
            onNavigateToDownload={handleNavigateToDownload}
            manualMatchFolder={manualMatchFolder}
            onConfirmMatch={handleConfirmMatch}
          />
        ) : (
          <>
            {view === "tracking" && (
              <TrackingPage onSelectAnime={handleSelectAnime} />
            )}
            {view === "search" && (
              <SearchPage
                onSelectAnime={handleSelectAnime}
                manualMatchFolder={manualMatchFolder}
              />
            )}
            {view === "download" && (
              <DownloadPage
                key={downloadPrefill ? JSON.stringify(downloadPrefill) : "empty"}
                initialKeyword={downloadPrefill?.keyword}
                initialBgmId={downloadPrefill?.bgmId ?? null}
                initialSubscribe={downloadPrefill?.subscribe}
                onBack={
                  downloadPrefill?.bgmId != null
                    ? () => setSelectedBgmId(downloadPrefill.bgmId)
                    : undefined
                }
              />
            )}
            {view === "downloadManager" && <DownloadManagerPage />}
            {view === "library" && (
              <LibraryPage
                onSelectAnime={handleSelectAnime}
                onManualMatch={handleManualMatch}
              />
            )}
            {view === "rss" && <RssPage />}
            {view === "settings" && (
              <SettingsPage
                ref={settingsRef}
                onReconfigureQbittorrent={() => setQbitSetupCompleted(false)}
              />
            )}
          </>
        )}
      </main>

      {pendingView !== null && (
        <div className="fixed inset-0 z-50 flex items-center justify-center bg-ink/60">
          <div className="w-80 rounded-md border border-border bg-surface p-5">
            <div className="mb-1 text-sm">有未保存的修改</div>
            <p className="mb-4 font-mono text-[11px] text-muted">
              离开设置页前,要保存刚才的修改吗?
            </p>
            <div className="flex flex-col gap-2">
              <button
                onClick={handleSaveAndLeaveSettings}
                className="rounded border border-vermillion px-3 py-1.5 font-mono text-xs text-vermillion transition-colors hover:bg-vermillion hover:text-ink"
              >
                保存并离开
              </button>
              <button
                onClick={handleDiscardAndLeaveSettings}
                className="rounded border border-border px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:border-vermillion hover:text-vermillion"
              >
                不保存,直接离开
              </button>
              <button
                onClick={handleCancelLeaveSettings}
                className="rounded px-3 py-1.5 font-mono text-xs text-muted transition-colors hover:text-paper"
              >
                取消
              </button>
            </div>
          </div>
        </div>
      )}
    </div>
  );
}

export default App;
