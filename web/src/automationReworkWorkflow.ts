import type { AutomationReworkStatusItem } from "./types";

export interface AutomationReworkPresentation {
  label: string;
  tone: "neutral" | "warning" | "danger";
  explanation: string;
}

export function automationReworkPresentation(
  item: AutomationReworkStatusItem,
): AutomationReworkPresentation {
  if (
    item.integrity_error_code !== null ||
    item.command_delivery_status === "INTEGRITY_FAILED"
  ) {
    return {
      label: "НАРУШЕНА ЦЕЛОСТНОСТЬ",
      tone: "danger",
      explanation:
        "Связь запроса и команды не подтверждена. Выпуск остаётся заблокирован до расследования.",
    };
  }
  if (item.status === "PENDING_DISPATCH") {
    return {
      label: "ОЖИДАЕТ ЗАПУСКА",
      tone: "warning",
      explanation:
        "Запрос сохранён, но автоматический диспетчер ещё не подтвердил передачу этапу.",
    };
  }
  if (item.status === "BLOCKED") {
    return {
      label: "ЗАБЛОКИРОВАНО",
      tone: "danger",
      explanation:
        "Система не может выполнить эту доработку автоматически. Выпуск остаётся заблокирован.",
    };
  }
  if (item.command_delivery_status === "DEAD_LETTERED") {
    return {
      label: "ОШИБКА ЗАПУСКА",
      tone: "danger",
      explanation:
        "Команда не была обработана после допустимых повторов. Выпуск остаётся заблокирован.",
    };
  }
  if (item.command_delivery_status === "PROCESSING") {
    return {
      label: "ПЕРЕДАЁТСЯ ЭТАПУ",
      tone: "warning",
      explanation:
        "Команда взята обработчиком. Это ещё не подтверждает завершение перерасчёта.",
    };
  }
  if (item.command_delivery_status === "ACKNOWLEDGED") {
    return {
      label: "КОМАНДА ПРИНЯТА",
      tone: "neutral",
      explanation:
        "Этап принял команду. Готовность появится только после новых проверяемых результатов.",
    };
  }
  return {
    label: "КОМАНДА В ОЧЕРЕДИ",
    tone: "warning",
    explanation:
      "Диспетчер проверил запрос и поставил команду в очередь. Перерасчёт ещё не завершён.",
  };
}
