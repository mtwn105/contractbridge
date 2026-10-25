import type { Metadata } from "next";
import { RepairConsole } from "./repair-console";
import "./console.css";

export const metadata: Metadata = {
  title: "ContractBridge — Integration repair console",
  description: "Follow temporary runtime recovery and permanent API client repairs.",
};

export default function Home() {
  return <RepairConsole />;
}
