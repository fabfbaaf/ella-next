import React, { lazy, Suspense } from "react";
import { createRoot } from "react-dom/client";
const AdminWindow = lazy(() => import("./features/admin/AdminWindow").then((module) => ({ default: module.AdminWindow })));
const PetWindow = lazy(() => import("./features/pet/PetWindow").then((module) => ({ default: module.PetWindow })));
import "./styles.css";

const isAdmin = new URLSearchParams(window.location.search).get("window") === "admin";
document.body.className = isAdmin ? "admin-body" : "pet-body";
createRoot(document.getElementById("root")!).render(
  <React.StrictMode><Suspense fallback={null}>{isAdmin ? <AdminWindow /> : <PetWindow />}</Suspense></React.StrictMode>,
);
