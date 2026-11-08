>D
; bitShake base script, adapted to EMH eHZB-W22E8 (full data set, InF on)
>B
=>sensor53 r
>M 1
+1,5,s,0,9600,eHZ,4
1,=soC,1024,0
1,=so1,00010800,63,5,63,5,000f0700
1,77070100010800ff@1000,Verbrauch,kWh,ImportActive,3
1,77070100020800ff@1000,Einspeisung,kWh,ExportActive,3
1,77070100100700ff@1,akt. Leistung,W,Power,0
1,=h--
1,77070100240700ff@1,Leistung L1,W,Power_L1,0
1,77070100380700ff@1,Leistung L2,W,Power_L2,0
1,770701004c0700ff@1,Leistung L3,W,Power_L3,0
1,77070100200700ff@1,Spannung L1,V,Voltage_L1,1
1,77070100340700ff@1,Spannung L2,V,Voltage_L2,1
1,77070100480700ff@1,Spannung L3,V,Voltage_L3,1
1,770701001f0700ff@1,Strom L1,A,Current_L1,2
1,77070100330700ff@1,Strom L2,A,Current_L2,2
1,77070100470700ff@1,Strom L3,A,Current_L3,2
1,770701000e0700ff@1,Frequenz,Hz,Frequency,1
#
