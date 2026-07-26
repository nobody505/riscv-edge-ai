#include "asr.h"
extern "C"{ void * __dso_handle = 0 ;}
#include "setup.h"
#include "myLib/asr_event.h"

uint32_t snid;

//{speak:小蝶-清新女声,vol:10,speed:10,platform:haohaodada}
//{playid:10001,voice:}
//{playid:10002,voice:}

void ASR_CODE()
{
  //{ID:10500,keyword:"唤醒词",ASR:"小空小空",ASRTO:""}
  if(snid == 10500){
    Serial.println("WAKE");
  }
  //{ID:10501,keyword:"命令词",ASR:"打开出行模式",ASRTO:""}
  if(snid == 10501){
    Serial.println("TRAVEL_ON");
  }
  //{ID:10502,keyword:"命令词",ASR:"关闭出行模式",ASRTO:""}
  if(snid == 10502){
    Serial.println("TRAVEL_OFF");
  }
  //{ID:10503,keyword:"命令词",ASR:"打开行路模式",ASRTO:""}
  if(snid == 10503){
    Serial.println("TRAVEL_ON");
  }
  //{ID:10504,keyword:"命令词",ASR:"关闭行路模式",ASRTO:""}
  if(snid == 10504){
    Serial.println("TRAVEL_OFF");
  }
  //{ID:10505,keyword:"命令词",ASR:"打开行走模式",ASRTO:""}
  if(snid == 10505){
    Serial.println("TRAVEL_ON");
  }
  //{ID:10506,keyword:"命令词",ASR:"关闭行走模式",ASRTO:""}
  if(snid == 10506){
    Serial.println("TRAVEL_OFF");
  }
  //{ID:10507,keyword:"命令词",ASR:"打开出门模式",ASRTO:""}
  if(snid == 10507){
    Serial.println("TRAVEL_ON");
  }
  //{ID:10508,keyword:"命令词",ASR:"关闭出门模式",ASRTO:""}
  if(snid == 10508){
    Serial.println("TRAVEL_OFF");
  }
  //{ID:10509,keyword:"命令词",ASR:"打开灯带",ASRTO:""}
  if(snid == 10509){
    Serial.println("LIGHT_ON");
  }
  //{ID:10510,keyword:"命令词",ASR:"关闭灯带",ASRTO:""}
  if(snid == 10510){
    Serial.println("LIGHT_OFF");
  }
  //{ID:10511,keyword:"命令词",ASR:"关闭警报",ASRTO:""}
  if(snid == 10511){
    Serial.println("FALL_ACK");
  }
  set_state_enter_wakeup(15000);
}

void hardware_init(){
  vol_set(7);
  vTaskDelete(NULL);
}

void setup()
{
  digital_write(4, 1);
  set_gpio_input(29);
  setPinFun(13,SECOND_FUNCTION);
  setPinFun(14,SECOND_FUNCTION);
  Serial.begin(115200);
}
