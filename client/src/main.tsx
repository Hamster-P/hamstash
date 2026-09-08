import React from "react";
import ReactDOM from "react-dom/client";
import App from "./App";
import { initApiBase } from "./api";

// 渲染前先把后端端口解析好(见 api.ts):所有页面的 API_BASE 都从那个模块读,
// 这一步做完之后它们拿到的才是用户实际配置的端口。
initApiBase().finally(() => {
  ReactDOM.createRoot(document.getElementById("root") as HTMLElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
});
