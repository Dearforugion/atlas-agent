# Повышение привилегий
Когда: есть доступ к машине — стать root/administrator.
Linux: sudo -l; find / -perm -4000 (SUID); crontab -l; gtfobins.github.io; конфиги с паролями (.env, wp-config.php); uname -a → searchsploit → metasploit_run.
Windows: whoami /priv (SeImpersonate → Potato), wmic service; незакреплённые службы, AlwaysInstallElevated, пароли в реестре.
Пруф: было user → стало root, вектор. Защита: патчи, минимум прав.