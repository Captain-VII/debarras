//! Cœur de Débarras, porté depuis le paquet Python `core/` (voir le plan de migration).
//! Aucune dépendance à l'interface : la couche Tauri appelle ces modules.

pub mod cache;
pub mod duplicates;
pub mod filetypes;
pub mod finder;
pub mod paths;
pub mod safety;
pub mod samename;
pub mod scanner;
