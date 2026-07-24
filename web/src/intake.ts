export interface ProjectDraft {
  code: string;
  name: string;
  reason: string;
  acknowledged: boolean;
}

export interface DocumentUploadDraft {
  logicalKey: string;
  title: string;
  documentType: string;
  revisionLabel: string;
  reason: string;
  file: File | null;
  acknowledged: boolean;
}

export interface DocumentSetConfirmationDraft {
  reason: string;
  projectCode: string;
  acknowledged: boolean;
}

export function validateProjectDraft(draft: ProjectDraft): string | null {
  const code = draft.code.trim();
  if (!code || !draft.name.trim() || !draft.reason.trim()) {
    return "Заполните шифр, наименование и основание создания проекта";
  }
  if (code.length > 128 || draft.name.trim().length > 500) {
    return "Шифр или наименование превышает допустимую длину";
  }
  if (/\s/.test(code)) {
    return "Шифр проекта не должен содержать пробелы";
  }
  if (draft.reason.trim().length > 2000) {
    return "Основание создания превышает 2000 символов";
  }
  if (!draft.acknowledged) {
    return "Подтвердите организацию и идентификаторы создаваемого проекта";
  }
  return null;
}

export function validateDocumentUploadDraft(
  draft: DocumentUploadDraft,
  maxUploadBytes: number,
): string | null {
  if (
    !draft.logicalKey.trim() ||
    !draft.title.trim() ||
    !draft.documentType.trim() ||
    !draft.revisionLabel.trim() ||
    !draft.reason.trim()
  ) {
    return "Заполните метаданные документа и основание загрузки";
  }
  if (/\s/.test(draft.logicalKey.trim())) {
    return "Логический ключ документа не должен содержать пробелы";
  }
  if (
    draft.logicalKey.trim().length > 300 ||
    draft.title.trim().length > 1000 ||
    draft.documentType.trim().length > 100 ||
    draft.revisionLabel.trim().length > 100 ||
    draft.reason.trim().length > 2000
  ) {
    return "Одно из текстовых полей превышает допустимую длину";
  }
  if (draft.file === null) {
    return "Выберите исходный файл";
  }
  if (draft.file.size > maxUploadBytes) {
    return "Файл превышает настроенный серверный лимит";
  }
  if (!draft.acknowledged) {
    return "Подтвердите редакцию, критичность и назначение файла";
  }
  return null;
}

export function validateDocumentSetConfirmationDraft(
  draft: DocumentSetConfirmationDraft,
  expectedProjectCode: string,
): string | null {
  if (!draft.reason.trim()) {
    return "Укажите основание независимой проверки комплекта документов";
  }
  if (draft.reason.trim().length > 2000) {
    return "Основание проверки превышает 2000 символов";
  }
  if (draft.projectCode !== expectedProjectCode) {
    return "Введите точный шифр проекта для подтверждения действия";
  }
  if (!draft.acknowledged) {
    return "Подтвердите сверку состава, редакций и хеша манифеста";
  }
  return null;
}
