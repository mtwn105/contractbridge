import type { Metadata } from "next";
import Shop from "@/components/shop";

export const metadata: Metadata = { title: "Fieldwork Supply — ContractBridge example" };

export default function ShopExample() {
  return <Shop />;
}
